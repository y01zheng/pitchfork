import angr
import pyvex
import claripy
from angr.errors import SimReliftException, UnsupportedIRStmtError
from angr.state_plugins.inspect import BP_AFTER, BP_BEFORE
from angr.engines import vex

import collections

import logging
l = logging.getLogger(name=__name__)

class SimEngineSpecVEX(angr.SimEngineVEX):
    """
    Execution engine which allows bounded wrong-path speculation.
    Based on the default SimEngineVEX.
    """

    def _handle_statement(self, state, successors, stmt):
        """
        An override of the _handle_statement method in SimEngineVEX base class.
        Much code copied from there; see SimEngineVEX class for more information/docs.
        """

        if type(stmt) == pyvex.IRStmt.IMark:
            ins_addr = stmt.addr + stmt.delta
            state.scratch.ins_addr = ins_addr

            # Raise an exception if we're suddenly in self-modifying code
            for subaddr in range(stmt.len):
                if subaddr + stmt.addr in state.scratch.dirty_addrs:
                    raise SimReliftException(state)
            state._inspect('instruction', BP_AFTER)

            #l.debug("IMark: %#x", stmt.addr)
            state.scratch.num_insns += 1
            state._inspect('instruction', BP_BEFORE, instruction=ins_addr)

            if state.spec.mispredicted:
                return False  # report path as deadended

        # process it!
        try:
            stmt_handler = self.stmt_handlers[stmt.tag_int]
        except IndexError:
            l.error("Unsupported statement type %s", (type(stmt)))
            if angr.options.BYPASS_UNSUPPORTED_IRSTMT not in state.options:
                raise UnsupportedIRStmtError("Unsupported statement type %s" % (type(stmt)))
            state.history.add_event('resilience', resilience_type='irstmt', stmt=type(stmt).__name__, message='unsupported IRStmt')
            return None
        else:
            exit_data = stmt_handler(self, state, stmt)

        # handling conditional exits is where the magic happens
        if exit_data is not None:
            target, guard, jumpkind = exit_data

            l.debug("time {}: forking for conditional exit to {} under guard {}".format(state.spec.ins_executed, target, guard))

            # Unlike normal SimEngineVEX, we always proceed down both sides of the branch
            # (to simulate possible wrong-path execution, i.e. branch misprediction)
            # and add the path constraints later, only after _spec_window_size instructions have passed

            exit_state = state.copy()
            cont_state = state

            branchcond = guard
            notbranchcond = claripy.Not(branchcond)
            if not state.solver.is_true(branchcond): exit_state.spec.conditionals.append(branchcond)  # don't bother adding a deferred 'True' constraint
            if not state.solver.is_true(notbranchcond): cont_state.spec.conditionals.append(notbranchcond)  # don't bother adding a deferred 'True' constraint

            successors.add_successor(exit_state, target, guard, jumpkind, add_guard=False,
                                    exit_stmt_idx=state.scratch.stmt_idx, exit_ins_addr=state.scratch.ins_addr)

            # We don't add the guard for the exit_state (add_guard=False).
            # Unfortunately, the call to add the 'default' successor at the end of an irsb
            # (line 313 in vex/engine.py as of this writing) leaves add_guard as default (True).
            # For the moment, rather than patching this, we just don't record the guard at
            # all on the cont_state.
            # TODO not sure if this will mess us up. Is scratch.guard used for merging?
            # Haven't thought about how speculation should interact with merging.
            # More fundamentally, what is scratch.guard used for when add_guard=False? Anything?
            #cont_state.scratch.guard = claripy.And(cont_state.scratch.guard, notbranchcond)

        return True

class SpecState(angr.SimStatePlugin):
    def __init__(self, spec_window_size, ins=0, conditionals=None):
        super().__init__()
        self._spec_window_size = spec_window_size
        self.ins_executed = ins
        if conditionals is not None:
            self.conditionals = conditionals
        else:
            self.conditionals = SpecQueue(ins)
        self.mispredicted = False

    def arm(self, state):
        state.inspect.b('instruction', when=BP_BEFORE, action=tickSpecState)
        state.inspect.b('statement', when=BP_BEFORE, action=handleFences)

    @angr.SimStatePlugin.memo
    def copy(self, memo):
        return SpecState(spec_window_size=self._spec_window_size, ins=self.ins_executed, conditionals=self.conditionals.copy())

    def tick(self):
        # we count instructions executed here because I couldn't find an existing place (e.g. state.history) where instructions are counted.
        # (TODO state.scratch.num_insns? is the 'scratch' reliably persistent?)
        # Also, this may miss instructions handled by other engines, but TODO that is presumably few?
        self.ins_executed += 1
        self.conditionals.tick()

class SpecQueue:
    """
    holds objects which are currently in-flight/unresolved
    """
    def __init__(self, ins_executed=0, q=None):
        self.ins_executed = ins_executed
        if q is None:
            self.q = collections.deque()
        else:
            self.q = q

    def copy(self):
        return SpecQueue(ins_executed=self.ins_executed, q=self.q.copy())

    def tick(self):
        self.ins_executed += 1

    def append(self, thing):
        self.q.append((thing, self.ins_executed))

    def ageOfOldest(self):
        if self.q:
            (_, whenadded) = self.q[0]  # peek
            return self.ins_executed - whenadded
        else:
            return None

    def popOldest(self):
        (thing, _) = self.q.popleft()
        return thing

    def popAll(self):
        """
        A generator that pops each thing and yields it
        """
        while self.q:
            (thing, _) = self.q.popleft()
            yield thing

def tickSpecState(state):
    # Keep track of how many instructions we have executed
    state.spec.tick()

    # See if it is time to retire the oldest conditional, that is, end possible wrong-path execution
    age = state.spec.conditionals.ageOfOldest()
    while age and age > state.spec._spec_window_size:
        cond = state.spec.conditionals.popOldest()
        l.debug("time {}: adding deferred conditional (age {}): {}".format(state.spec.ins_executed, age, cond))
        state.add_constraints(cond)
        # See if the newly added constraint makes us unsat, if so, kill this state
        if angr.sim_options.LAZY_SOLVES not in state.options and not state.solver.satisfiable():
            l.debug("killing mispredicted path: constraints not satisfiable: {}".format(state.solver.constraints))
            state.spec.mispredicted = True
        age = state.spec.conditionals.ageOfOldest()  # check next conditional

def handleFences(state):
    """
    A hook watching for fence instructions, don't speculate past fences
    """
    stmt = state.scratch.irsb.statements[state.inspect.statement]
    if type(stmt) == pyvex.stmt.MBE and stmt.event == "Imbe_Fence":
        l.debug("time {}: encountered a fence, flushing all deferred constraints".format(state.spec.ins_executed))
        state.add_constraints(*list(state.spec.conditionals.popAll()))
        # See if this has made us unsat, if so, kill this state
        if angr.sim_options.LAZY_SOLVES not in state.options and not state.solver.satisfiable():
            l.debug("killing mispredicted path: constraints not satisfiable: {}".format(state.solver.constraints))
            state.spec.mispredicted = True
