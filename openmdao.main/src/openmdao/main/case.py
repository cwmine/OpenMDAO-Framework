
import time
from uuid import uuid1
from array import array
import traceback
from StringIO import StringIO
from inspect import getmro

from openmdao.main.expreval import ExprEvaluator
from openmdao.main.exceptions import TracedError, traceback_str
from openmdao.main.variable import is_legal_name

__all__ = ["Case"]

_Missing = object()

def _simpleflatten(name, obj):
    return [(name, obj)]

def _flatten_lst(name, lst):
    ret = []
    
    def _recurse_flatten(ret, name, idx, lst):
        for i,entry in enumerate(lst):
            new_idx = idx+[i]
            if isinstance(entry, (tuple, list, array)):
                _recurse_flatten(ret, name, new_idx, entry)
            else:
                idxstr = ''.join(["[%d]" % j for j in new_idx])
                ret.append(("%s%s" % (name, idxstr), entry))
    
    _recurse_flatten(ret, name, [], lst)
    return ret
        
flatteners = { # dict of functions that know how to 'flatten' a given object instance
       int: _simpleflatten,
       float: _simpleflatten,
       str: _simpleflatten,
       unicode: _simpleflatten,
       list: _flatten_lst,
       tuple: _flatten_lst,
       array: _flatten_lst,
    } 

def flatten_obj(name, obj):
    f = flatteners.get(type(obj))
    if f:
        return f(name, obj)
    for klass in getmro(type(obj))[1:]:
        if klass in flatteners:
            return flatteners[klass](name, obj)
    return []

class Case(object):
    """Contains all information necessary to specify an input *case*, i.e.,
    a list of names for all inputs to the case and their values. The case
    names may contain indexing into containers, attribute access, and/or
    function calls, as long as the full expression is valid as the
    left-hand side of an assignment. Outputs to be collected may also be
    added to the Case, and they can be more general expressions, i.e., they do
    not have to refer to a single variable. After the Case is executed, it
    will contain an indicator of the exit status of the case, a string
    containing error messages associated with the running of the case (if
    any), and a unique case identifier. 

    """
    def __init__(self, inputs=None, outputs=None, max_retries=None,
                 retries=None, label='', case_uuid=None, parent_uuid='', 
                 msg=None):
        """If inputs are supplied to the constructor, it must be an
        iterator that returns (name,value) tuples, where name is allowed
        to contain array notation and/or function calls. Outputs must be
        an iterator that returns strings containing names or expressions.
        
        """
        self._exprs = None
        self._outputs = None
        self._inputs = {}

        self.max_retries = max_retries  # times to retry after error(s)
        self.retries = retries          # times case was retried
        self.msg = msg                  # If non-null, error message.
                                        # Implies outputs are invalid. 
        self.exc = None                 # Exception during execution.
        self.label = label              # Optional label.
        if case_uuid:
            self.uuid = str(case_uuid)
        else:
            self.uuid = str(uuid1())  # unique identifier
        self.parent_uuid = str(parent_uuid)  # identifier of parent case, if any

        self.timestamp = time.time()

        if inputs: 
            self.add_inputs(inputs)
        if outputs:
            self.add_outputs(outputs)

    def __str__(self):
        if self._outputs:
            outs = self._outputs.items()
            outs.sort()
        else:
            outs = []
        ins = self._inputs.items()
        ins.sort()
        stream = StringIO()
        stream.write("Case: %s\n" % self.label)
        stream.write("   uuid: %s\n" % self.uuid)
        stream.write("   timestamp: %15f\n" % self.timestamp)
        if self.parent_uuid:
            stream.write("   parent_uuid: %s\n" % self.parent_uuid)

        if ins:
            stream.write("   inputs:\n")
            for name,val in ins:
                stream.write("      %s: %s\n" % (name,val))
        if outs:
            stream.write("   outputs:\n")
            for name,val in outs:
                stream.write("      %s: %s\n" % (name,val))
        if self.max_retries is not None:
            stream.write("   max_retries: %s\n" % self.max_retries)
        if self.retries is not None:
            stream.write("   retries: %s\n" % self.retries)
        if self.msg:
            stream.write("   msg: %s\n" % self.msg)
        if self.exc is not None:
            stream.write("   exc: %s\n" % traceback_str(self.exc))
        return stream.getvalue()
    
    def __eq__(self, other): 
        if self is other:
            return True
        try:
            if self.msg != other.msg or self.label != other.label:
                return False
            if len(self) != len(other):
                return False
            for selftup, othertup in zip(self.items(flatten=True), 
                                         other.items(flatten=True)):
                if selftup[0] != othertup[0] or selftup[1] != othertup[1]:
                    return False
        except:
            return False
        return True

    def __getitem__(self, name):
        val = self._inputs.get(name, _Missing)
        if val is not _Missing:
            return val
        if self._outputs:
            return self._outputs[name]
        raise KeyError("'%s' not found" % name)
    
    def __setitem__(self, name, value):
        if self._outputs and name in self._outputs:
            self._outputs[name] = value
        elif name in self._inputs:
            self._inputs[name] = value
        else:
            raise KeyError("'%s' not found" % name)
    
    def __contains__(self, name):
        return name in self._inputs or (self._outputs and name in self._outputs)
    
    def __len__(self):
        if self._outputs is None:
            return len(self._inputs)
        else:
            return len(self._inputs) + len(self._outputs)
    
    def get_input(self, name):
        return self._inputs[name]
    
    def get_output(self, name):
        if self._outputs:
            return self._outputs[name]
        raise KeyError("'%s' not found" % name)
    
    def get_inputs(self, flatten=False):
        if flatten:
            ret = []
            for k,v in self._inputs.items():
                ret.extend(flatten_obj(k, v))
            return ret
        else:
            return self._inputs.items()
        
    def get_outputs(self, flatten=False):
        if flatten:
            ret = []
            for k,v in self._outputs.items():
                ret.extend(flatten_obj(k, v))
            return ret
        else:
            return self._outputs.items()
        
    def items(self, iotype=None, flatten=False):
        """Return a list of (name,value) tuples for variables/expressions in this Case.
        
        iotype: str or None
            If 'in', only inputs are returned.
            If 'out', only outputs are returned.
            If None (the default), inputs and outputs are returned.
            
        flatten: bool
            If True, split multi-part Variables (like VariableTrees and Arrays) into
            their constituents.
        """
        if iotype is None:
            if self._outputs:
                return self.get_inputs(flatten) + self.get_outputs(flatten)
            else:
                return self.get_inputs(flatten)
        elif iotype == 'in':
            return self.get_inputs(flatten)
        elif iotype == 'out':
            if self._outputs:
                return self.get_outputs(flatten)
            else:
                return []
        else:
            raise NameError("invalid iotype arg (%s). Must be 'in','out',or None" % str(iotype))
        
    def keys(self, iotype=None, flatten=False):
        """Return a list of name/expression strings for this Case.
        
        iotype: str or None
            If 'in', only inputs are returned.
            If 'out', only outputs are returned.
            If None (the default), inputs and outputs are returned.
        """
        return [k for k,v in self.items(iotype, flatten=flatten)]
        
    def values(self, iotype=None, flatten=False):
        """Return a list of values for this Case.
        
        iotype: str or None
            If 'in', only inputs are returned.
            If 'out', only outputs are returned
            If None (the default), inputs and outputs are returned
        """
        return [v for k,v in self.items(iotype, flatten=flatten)]
    
    def reset(self):
        """Remove any saved output values, set retries to None, get a new uuid
        and reset the parent_uuid.  Essentially this Case becomes like a new 
        Case with the same set of inputs and outputs that hasn't been executed
        yet.
        """
        self.parent_uuid = ''
        self.uuid = str(uuid1())
        self.retries = None
        for key in self._outputs.keys():
            self._outputs[key] = _Missing

    def apply_inputs(self, scope):
        """Take the values of all of the inputs in this case and apply them
        to the specified scope.
        """
        scope._case_id = self.uuid
        if self._exprs:
            for name,value in self._inputs.items():
                expr = self._exprs.get(name)
                if expr:
                    expr.set(value, scope)
                else:
                    scope.set(name, value)
        else:
            for name,value in self._inputs.items():
                scope.set(name, value)

    def update_outputs(self, scope, msg=None):
        """Update the value of all outputs in this Case, using the given scope.
        """
        self.msg = msg
        last_excpt = None
        if self._outputs is not None:
            if self._exprs:
                for name in self._outputs.keys():
                    expr = self._exprs.get(name)
                    try:
                        if expr:
                            self._outputs[name] = expr.evaluate(scope)
                        else:
                            self._outputs[name] = scope.get(name)
                    except Exception as err:
                        last_excpt = TracedError(err, traceback.format_exc())
                        self._outputs[name] = _Missing
                        if self.msg is None:
                            self.msg = str(err)
                        else:
                            self.msg = self.msg + " %s" % err
            else:
                for name in self._outputs.keys():
                    try:
                        self._outputs[name] = scope.get(name)
                    except Exception as err:
                        last_excpt = TracedError(err, traceback.format_exc())
                        self._outputs[name] = _Missing
                        if self.msg is None:
                            self.msg = str(err)
                        else:
                            self.msg = self.msg + " %s" % err

        self.timestamp = time.time()

        if last_excpt is not None:
            raise last_excpt
            
    def add_input(self, name, value):
        """Adds an input and its value to this case.
        
        name: str
            Name of the input to be added. May contain an expression as long
            as it is valid when placed on the left-hand side of an assignment.
            
        value: 
            Value that the input will be assigned to.
        """
        self._register_expr(name)
        self._inputs[name] = value
        
    def add_inputs(self, inp_iter):
        """Adds multiple inputs to this case.
        
        inp_iter: Iterator returning (name,value)
            Iterator of input names and values.
        """
        for name, value in inp_iter:
            self.add_input(name, value)

    def add_output(self, name, value=_Missing):
        """Adds an output to this case.
        
        name: str
            Name of output to be added.
        """
        self._register_expr(name)
        if self._outputs is None:
            self._outputs = { name: value }
        else:
            self._outputs[name] = value
        
    def add_outputs(self, outputs):
        """Adds outputs to this case.
        
        outputs: iterator returning names or tuples of the form (name,value)
            outputs to be added
        """
        for entry in outputs:
            if isinstance(entry, basestring):
                self.add_output(entry)
            else: # assume it's a tuple of the form (name, value)
                self.add_output(entry[0], entry[1])
                
    def subcase(self, names):
        """Return a new Case having a specified subset of this Case's inputs
        and outputs.
        """
        ins = []
        outs = []
        for name in names:
            val =  self._inputs.get(name)
            if val is not None:
                ins.append((name,val))
            elif self._outputs:
                outs.append((name,self._outputs[name]))
            else:
                raise KeyError("'%s' is not part of this Case" % name)
        sc =  Case(inputs=ins, outputs=outs, parent_uuid=self.parent_uuid,
                    max_retries=self.max_retries)
        sc.timestamp = self.timestamp
        return sc

        
    def _register_expr(self, s):
        """If the given string contains an expression, create an ExprEvaluator and
        store it in self._exprs.
        """
        if not is_legal_name(s):
            expr =  ExprEvaluator(s)
            if self._exprs is None:
                self._exprs = {}
            self._exprs[s] = expr

