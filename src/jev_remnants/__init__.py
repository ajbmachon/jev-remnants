"""jvr: find the residual mentions of a removal that bring it back.

Code collects candidates and owns the action policy; Jev answers two closed
questions per mention: does it refer to the removed thing, and would an agent
reading it be led to use or re-create it.
"""

from .questions import LEADS_RECREATION, REFERS_REMOVED
from .scan import run_scan

__all__ = ["LEADS_RECREATION", "REFERS_REMOVED", "run_scan"]
