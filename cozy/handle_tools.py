"""
Handles (aka heap-allocated objects) require careful treatment since everything
else in Cozy is relatively pure.  This module defines some useful functions for
handling handles.
"""

from collections import OrderedDict

from cozy.common import typechecked, extend
from cozy.target_syntax import *
from cozy.syntax_tools import fresh_var, mk_lambda, all_exps, BottomUpRewriter, deep_copy, free_vars
from cozy.typecheck import is_collection, is_scalar, retypecheck

@typechecked
def _merge(a : {THandle:Exp}, b : {THandle:Exp}) -> {THandle:Exp}:
    """NOTE: assumes ownership of `a`"""
    res = a
    for k, vb in b.items():
        va = res.get(k)
        if va is None:
            res[k] = vb
        else:
            res[k] = EBinOp(va, "+", vb).with_type(va.type)
    return res

@typechecked
def reachable_handles_by_type(root : Exp) -> {THandle:Exp}:
    """
    Compute a mapping from handle types to bags of all handle objects of that
    type reachable from the root given root.

    Note that the bags may contain duplicate handles.
    """
    if isinstance(root.type, THandle):
        return _merge(
            { root.type : ESingleton(root).with_type(TBag(root.type)) },
            reachable_handles_by_type(EGetField(root, "val").with_type(root.type.value_type)))
    elif is_collection(root.type):
        v = fresh_var(root.type.t)
        res = reachable_handles_by_type(v)
        for k, bag in list(res.items()):
            res[k] = EFlatMap(root, ELambda(v, bag)).with_type(bag.type)
        return res
    elif isinstance(root.type, TTuple):
        res = OrderedDict()
        for i, t in enumerate(root.type.ts):
            res = _merge(res, reachable_handles_by_type(ETupleGet(root, i).with_type(t)))
        return res
    elif isinstance(root.type, TRecord):
        res = OrderedDict()
        for f, t in root.type.fields:
            res = _merge(res, reachable_handles_by_type(EGetField(root, f).with_type(t)))
        return res
    elif isinstance(root.type, TMap):
        raise NotImplementedError()
    else:
        return OrderedDict()

@typechecked
def reachable_handles_at_method(spec : Spec, m : Method) -> {THandle:Exp}:
    res = OrderedDict()
    for v, t in spec.statevars:
        res = _merge(res, reachable_handles_by_type(EVar(v).with_type(t)))
    for v, t in m.args:
        res = _merge(res, reachable_handles_by_type(EVar(v).with_type(t)))
    return res

def EForall(e, p):
    return EUnaryOp(UOp.All, EMap(e, mk_lambda(e.type.t, p)).with_type(type(e.type)(BOOL))).with_type(BOOL)

@typechecked
def implicit_handle_assumptions_for_method(handles : {THandle:Exp}, m : Method) -> [Exp]:
    # for instance: implicit_handle_assumptions_for_method(reachable_handles_at_method(spec, m), m)
    new_assumptions = []
    for t, bag in handles.items():
        new_assumptions.append(
            EForall(bag, lambda h1: EForall(bag, lambda h2:
                EImplies(EEq(h1, h2),
                    EEq(EGetField(h1, "val").with_type(h1.type.value_type),
                        EGetField(h2, "val").with_type(h2.type.value_type))))))
    return new_assumptions

def fix_ewithalteredvalue(e : Exp):
    """
    EWithAlteredValue gives us real trouble.
    This rewrites `e` to avoid them, but may result in a much slower
    implementation.
    """
    # TODO: should we do this earlier? Maybe at incrementalization time?

    if not any(isinstance(x, EWithAlteredValue) for x in all_exps(e)):
        # Good! This is the dream.
        return e

    def do(e, t):
        """
        Rewrite handles into (h, val) tuples.
        """
        if is_collection(t):
            return EMap(e, mk_lambda(t.t, lambda x: do(x, t.t)))
        elif isinstance(t, THandle):
            return ETuple((e, EGetField(e, "val")))
        elif is_scalar(t):
            return e
        else:
            raise NotImplementedError(t)

    def undo(e, t):
        """
        Rewrite (h, val) tuples into handles.
        """
        if is_collection(t):
            return EMap(e, mk_lambda(t.t, lambda x: undo(x, t.t)))
        elif isinstance(t, THandle):
            return ETupleGet(e, 0)
        elif is_scalar(t):
            return e
        else:
            raise NotImplementedError(t)

    class V(BottomUpRewriter):
        def __init__(self, fvs):
            self.should_rewrite = { v : True for v in fvs }
        def visit_ELambda(self, f):
            with extend(self.should_rewrite, f.arg, False):
                return self.join(f, (f.arg, self.visit(f.body)))
        def visit_EVar(self, v):
            return do(v, v.type) if self.should_rewrite[v] else v
        def visit_EWithAlteredValue(self, e):
            return ETuple((
                ETupleGet(self.visit(e.handle), 0),
                self.visit(e.new_value)))
        def visit_EGetField(self, e):
            # print("rewriting {}... ".format(pprint(e)), end="")
            if isinstance(e.e.type, THandle):
                assert e.f == "val"
                # print("ELIM")
                return ETupleGet(self.visit(e.e), 1).with_type(e.type)
            else:
                # print("NOOP; t={}".format(pprint(e.e.type)))
                return EGetField(self.visit(e.e), e.f).with_type(e.type)

    # print("FIXING {}".format(pprint(e)))
    e = deep_copy(e)
    e = undo(V(free_vars(e)).visit(e), e.type)
    # print("-----> {}".format(pprint(e)))
    res = retypecheck(e)
    assert res
    return e
