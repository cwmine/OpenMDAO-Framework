import atexit
import os.path
import Queue
import sys
import threading

from enthought.traits.api import Range, Bool, Instance

from openmdao.main.api import Component, Driver
from openmdao.main.exceptions import RunStopped
from openmdao.main.interfaces import ICaseIterator
from openmdao.main.resource import ResourceAllocationManager as RAM
from openmdao.util.filexfer import filexfer

__all__ = ('CaseIteratorDriver',)

_EMPTY    = 'empty'
_READY    = 'ready'
_COMPLETE = 'complete'
_ERROR    = 'error'

class ServerError(Exception):
    """ Raised when a server thread has problems. """
    pass


class CaseIteratorDriver(Driver):
    """
    Run a set of cases provided by an :class:`ICaseIterator` in a manner similar
    to the ROSE framework. Concurrent evaluation is supported, with the various
    evaluations executed across servers obtained from the
    :class:`ResourceAllocationManager`.

    - The `iterator` socket provides the cases to be evaluated.
    - The `model` socket provides the model to be executed.
    - The `recorder` socket is used to record results.
    - If `sequential` is True, then the cases are evaluated sequentially.
    - If `reload_model` is True, the model is reloaded between executions.
    - `max_retries` sets the number of times to retry a failed case.

    .. parsed-literal::

       TODO: define interface for 'recorder'.
       TODO: support stepping and resuming execution.
       TODO: improve response to a stop request.

    """

    iterator = Instance(ICaseIterator, desc='Cases to evaluate.', required=True)
    recorder = Instance(object, desc='Something to append() to.', required=True)
    model = Instance(Component, desc='Model to be executed.', required=True)
    
    sequential = Bool(True, iostatus='in',
                      desc='Evaluate cases sequentially.')

    reload_model = Bool(True, iostatus='in',
                        desc='Reload model between executions.')

    max_retries = Range(value=1, low=0, iostatus='in',
                        desc='Number of times to retry a case.')

    def __init__(self, *args, **kwargs):
        super(CaseIteratorDriver, self).__init__(*args, **kwargs)

        self._iter = None
        self._replicants = 0

        self._egg_file = None
        self._egg_required_distributions = None
        self._egg_orphan_modules = None
        self._eggs_used = []

        # Unpickleable objects.
        self._reply_queue = None
        self._server_lock = None

        # Various per-server data keyed by server name.
        self._servers = {}
        self._top_levels = {}
        self._server_info = {}
        self._queues = {}
        self._in_use = {}
        self._server_states = {}
        self._server_cases = {}
        self._exceptions = {}

        self._todo = []   # Cases grabbed during server startup.
        self._rerun = []  # Cases that failed and should be retried.

        atexit.register(self._cleanup_eggs)

    def _cleanup_eggs(self):
        """
        Cleanup any egg files still in existence at shutdown.
        This is needed because on @#$%^& Windows sometimes a closed ZipFile
        doesn't actually get released by the process.
        This appears to be related to concurrent startup, serializing has
        reduced (eliminated?) the problem.
        """
        for egg in self._eggs_used:
            if os.path.exists(egg):
                try:
                    os.remove(egg)
                except WindowsError, exc:
                    print 'Warning: unable to remove egg:', exc

    def execute(self):
        """ Runs each case in `iterator` and records results in `recorder`. """
        try:
            self._start()
        finally:
            self._cleanup()
        if self._stop:
            self.raise_exception(RunStopped)

    def _start(self, replicate=True):
        """
        Start evaluating cases. If `replicate`, then replicate the model
        and save to an egg file first.
        """

        # Clear server data.
        self._servers = {}
        self._top_levels = {}
        self._queues = {}
        self._in_use = {}
        self._server_states = {}
        self._server_cases = {}
        self._exceptions = {}

        self._todo = []
        self._rerun = []

        self._iter = self.iterator.__iter__()

        if self.sequential:
            self.info('Start sequential evaluation.')
            try:
                self._todo.append(self._iter.next())
            except StopIteration:
                pass
            else:
                self._server_cases[None] = None
                self._server_states[None] = _EMPTY
                while self._server_ready(None):
                    pass
        else:
            self.info('Start concurrent evaluation.')
            if replicate or self._egg_file is None:
                # Save model to egg.
                # Must do this before creating any locks or queues.
                self._replicants += 1
                version = 'replicant.%d' % (self._replicants)
                egg_info = self.model.save_to_egg(self.model.name, version)
                self._egg_file = egg_info[0]
                self._eggs_used.append(egg_info[0])
                self._egg_required_distributions = egg_info[1]
                self._egg_orphan_modules = [name for name, path in egg_info[2]]

            # Determine maximum number of servers available.
            resources = {
                'required_distributions':self._egg_required_distributions,
                'orphan_modules':self._egg_orphan_modules,
                'python_version':sys.version[:3]}
            max_servers = RAM.max_servers(resources)
            self.debug('max_servers %d', max_servers)
            if max_servers <= 0:
                msg = 'No servers supporting required resources %s' % resources
                self.raise_exception(msg, RuntimeError)

            # Kick off initial wave of cases.
            self._server_lock = threading.Lock()
            self._reply_queue = Queue.Queue()
            n_servers = 0
            while n_servers < max_servers:
                # Get next case. Limits servers started if max_servers > cases.
                try:
                    self._todo.append(self._iter.next())
                except StopIteration:
                    if not self._rerun:
                        break

                # Start server worker thread.
                n_servers += 1
                name = '%s_%d' % (self.name, n_servers)
                self.debug('starting worker for %s', name)
                self._servers[name] = None
                self._in_use[name] = True
                self._server_cases[name] = None
                self._server_states[name] = _EMPTY
                server_thread = threading.Thread(target=self._service_loop,
                                                 args=(name, resources))
                server_thread.daemon = True
                server_thread.start()

                if sys.platform == 'win32':
                    # Serialize startup, otherwise we have egg removal issues.
                    name, result = self._reply_queue.get()
                    if self._servers[name] is None:
                        self.debug('server startup failed for %s', name)
                        self._in_use[name] = False
                else:
                    # Process any pending events.
                    while self._busy():
                        try:
                            name, result = self._reply_queue.get(True, 0.1)
                        except Queue.Empty:
                            break  # Timeout.
                        else:
                            if self._servers[name] is None:
                                self.debug('server startup failed for %s', name)
                                self._in_use[name] = False
                            else:
                                self._in_use[name] = self._server_ready(name)

            if sys.platform == 'win32':
                # Kick-off serialized servers.
                for name in self._in_use.keys():
                    if self._in_use[name]:
                        self._in_use[name] = self._server_ready(name)

            # Continue until no servers are busy.
            while self._busy():
                name, result = self._reply_queue.get()
                self._in_use[name] = self._server_ready(name)

            # Shut-down (started) servers.
            for queue in self._queues.values():
                queue.put(None)
            for i in range(len(self._queues)):
                try:
                    name, status = self._reply_queue.get(True, 1)
                except Queue.Empty:
                    self.warning('Timeout waiting for %s to shut-down.', name)

            # Clean up unpickleables.
            self._reply_queue = None
            self._server_lock = None

    def _busy(self):
        """ Return True while at least one server is in use. """
        return any(self._in_use.values())

    def _cleanup(self):
        """ Cleanup egg file if necessary. """
        if self._egg_file and os.path.exists(self._egg_file):
            try:
                os.remove(self._egg_file)
            except WindowsError:
                # Closed ZipFile sometimes isn't released.
                # We'll try again at shutdown in _cleanup_eggs()
                pass
            self._egg_file = None

    def _server_ready(self, server):
        """
        Responds to asynchronous callbacks during :meth:`execute` to run cases
        retrieved from `iterator`.  Results are processed by `recorder`.
        Returns True if this server is still in use.
        """
        state = self._server_states[server]
        self.debug('server %s state %s', server, state)
        in_use = True

        if state == _EMPTY:
            try:
                self.debug('    load_model')
                self._load_model(server)
                self._server_states[server] = _READY
            except ServerError:
                self._server_states[server] = _ERROR

        elif state == _READY:
            # Test for stop request.
            if self._stop:
                self.debug('    stop requested')
                in_use = False

            # Select case to run.
            if self._todo:
                self.debug('    run startup case')
                self._run_case(self._todo.pop(0), server)
            elif self._rerun:
                self.debug('    rerun case')
                self._run_case(self._rerun.pop(0), server, rerun=True)
            else:
                try:
                    case = self._iter.next()
                except StopIteration:
                    self.debug('    no more cases')
                    in_use = False
                else:
                    self.debug('    run next case')
                    self._run_case(case, server)
        
        elif state == _COMPLETE:
            case = self._server_cases[server]
            self._server_cases[server] = None
            try:
                exc = self._model_status(server)
                if exc is None:
                    # Grab the data from the model.
                    for i, niv in enumerate(case.outputs):
                        try:
                            case.outputs[i] = (niv[0], niv[1],
                                self._model_get(server, niv[0], niv[1]))
                        except Exception, exc:
                            msg = "Exception getting '%s': %s" % (niv[0], exc)
                            case.msg = '%s: %s' % (self.get_pathname(), msg)
                else:
                    self.debug('    exception %s', exc)
                    case.msg = str(exc)
                # Record the data.
                self.recorder.append(case)

                if not case.msg:
                    if self.reload_model:
                        self.debug('    reload')
                        self._load_model(server)
                else:
                    self.debug('    load')
                    self._load_model(server)
                self._server_states[server] = _READY
            except ServerError:
                # Handle server error separately.
                self.debug('    server error')

        elif state == _ERROR:
            self._server_cases[server] = None
            try:
                self._load_model(server)
            except ServerError:
                pass  # Needs work!
            else:
                self._server_states[server] = _READY

        else:
            self.error('unexpected state %s for server %s', state, server)
            in_use = False

        return in_use

    def _run_case(self, case, server, rerun=False):
        """ Setup and run a case. """
        if not rerun:
            if not case.max_retries:
                case.max_retries = self.max_retries
            case.retries = 0

        case.msg = None
        self._server_cases[server] = case

        try:
            for name, index, value in case.inputs:
                try:
                    self._model_set(server, name, index, value)
                except Exception, exc:
                    msg = "Exception setting '%s': %s" % (name, exc)
                    self.raise_exception(msg, ServerError)
            self._model_execute(server)
            self._server_states[server] = _COMPLETE
        except ServerError, exc:
            self._server_states[server] = _ERROR
            if case.retries < case.max_retries:
                case.retries += 1
                self._rerun.append(case)
            else:
                case.msg = str(exc)
                self.recorder.append(case)

    def _service_loop(self, name, resource_desc):
        """ Each server has an associated thread executing this. """
        server, server_info = RAM.allocate(resource_desc)
        if server is None:
            self.error('Server allocation for %s failed :-(', name)
            self._reply_queue.put((name, False))
            return
        else:
            server_info['egg_file'] = None

        request_queue = Queue.Queue()

        with self._server_lock:
            self._servers[name] = server
            self._server_info[name] = server_info
            self._queues[name] = request_queue

        self._reply_queue.put((name, True))  # ACK startup.

        while True:
            request = request_queue.get()
            if request is None:
                break
            result = request[0](request[1])
            self._reply_queue.put((name, result))

        RAM.release(server)
        self._reply_queue.put((name, True))  # ACK shutdown.

    def _load_model(self, server):
        """ Load a model into a server. """
        if server is not None:
            self._queues[server].put((self._remote_load_model, server))
        return True

    def _remote_load_model(self, server):
        """ Load model into remote server. """
        egg_file = self._server_info[server].get('egg_file', None)
        if egg_file is not self._egg_file:
            # Only transfer if changed.
            filexfer(None, self._egg_file,
                     self._servers[server], self._egg_file, 'b')
            self._server_info[server]['egg_file'] = self._egg_file
        tlo = self._servers[server].load_model(self._egg_file)
        if not tlo:
            self.error("server.load_model of '%s' failed :-(",
                       self._egg_file)
            return False
        self._top_levels[server] = tlo
        return True

    def _model_set(self, server, name, index, value):
        """ Set value in server's model. """
        if server is None:
            self.model.set(name, value, index)
        else:
            self._top_levels[server].set(name, value, index)
        return True

    def _model_get(self, server, name, index):
        """ Get value from server's model. """
        if server is None:
            return self.model.get(name, index)
        else:
            return self._top_levels[server].get(name, index)

    def _model_execute(self, server):
        """ Execute model in server. """
        self._exceptions[server] = None
        if server is None:
            try:
                self.model.run()
            except Exception, exc:
                self._exceptions[server] = exc
                self.exception('Caught exception: %s' % exc)
        else:
            self._queues[server].put((self._remote_model_execute, server))

    def _remote_model_execute(self, server):
        """ Execute model in remote server. """
        try:
            self._top_levels[server].run()
        except Exception, exc:
            self._exceptions[server] = exc
            self.error('Caught exception from server %s, PID %d on %s: %s',
                       self._server_info[server]['name'],
                       self._server_info[server]['pid'],
                       self._server_info[server]['host'], exc)

    def _model_status(self, server):
        """ Return execute status from model. """
        return self._exceptions[server]

