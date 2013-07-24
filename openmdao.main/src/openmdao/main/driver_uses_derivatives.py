""" A driver with derivatives. Derived from Driver. Inherit from this
    if your driver requires gradients or Hessians.
    
    From a driver's perspective, derivatives are needed from its parameters
    to its objective(s) and constraints.
"""

# pylint: disable-msg=E0611,F0401
from openmdao.main.datatypes.api import Slot
from openmdao.main.driver import Driver

class DriverUsesDerivatives(Driver): 
    """This is Deprecated."""

    def __init__(self, *args, **kwargs):
        """ Deprecated."""
        
        logger.warning('DriverUsesDerivatives is deprecated. You can ' + 
                        'use Component instead.')
        
        super(DriverUsesDerivatives, self).__init__(*args, **kwargs)        
        
        
