
import pytest

from myia import operations
from myia.api import scalar_pipeline
from myia.infer import InferenceError
from myia.ir import Constant, isomorphic, GraphCloner
from myia.opt import PatternSubstitutionOptimization as psub, \
    PatternEquilibriumOptimizer, pattern_replacer, sexp_to_graph, \
    cse
from myia.prim import Primitive, ops as prim
from myia.utils import Merge
from myia.utils.unify import Var, var

from ..common import i64


X = Var('X')
Y = Var('Y')
V = var(lambda n: n.is_constant())


parse = scalar_pipeline \
    .configure({
        'convert.object_map': Merge({operations.getitem: prim.tuple_getitem})
    }) \
    .select('parse', 'resolve') \
    .make_transformer('input', 'graph')


# We will optimize patterns of these fake primitives


P = Primitive('P')
Q = Primitive('Q')
R = Primitive('R')


idempotent_P = psub(
    (P, (P, X)),
    (P, X),
    name='idempotent_P'
)

elim_R = psub(
    (R, X),
    X,
    name='elim_R'
)

Q0_to_R = psub(
    (Q, 0),
    (R, 0),
    name='Q0_to_R'
)

QP_to_QR = psub(
    (Q, (P, X)),
    (Q, (R, X)),
    name='QP_to_QR'
)

multiply_by_zero_l = psub(
    (prim.scalar_mul, 0, X),
    0,
    name='multiply_by_zero_l'
)

multiply_by_zero_r = psub(
    (prim.scalar_mul, X, 0),
    0,
    name='multiply_by_zero_r'
)

add_zero_l = psub(
    (prim.scalar_add, 0, X),
    X,
    name='add_zero_l'
)

add_zero_r = psub(
    (prim.scalar_add, X, 0),
    X,
    name='add_zero_r'
)


def _check_transform(before, after, transform):
    gbefore = parse(before)
    gbefore = GraphCloner(gbefore, total=True)[gbefore]
    gafter = parse(after)
    transform(gbefore)
    assert isomorphic(gbefore, gafter)


def _check_opt(before, after, *opts):
    eq = PatternEquilibriumOptimizer(*opts)
    _check_transform(before, after, eq)


def test_checkopt_is_cloning():
    # checkopt should optimize a copy of the before graph, which means
    # _check_opt(before, before) can only succeed if no optimizations are
    # applied.

    def before(x):
        return R(x)

    _check_opt(before, before)

    with pytest.raises(AssertionError):
        _check_opt(before, before,
                   elim_R)


def test_sexp_conversion():
    def f():
        return 10 * (5 + 4)

    sexp = (prim.scalar_mul, 10, (prim.scalar_add, 5, Constant(4)))

    g = sexp_to_graph(sexp)

    assert isomorphic(g, parse(f))


def test_elim():
    def before(x):
        return R(x)

    def after(x):
        return x

    _check_opt(before, after,
               elim_R)


def _idempotent_after(x):
    return P(x)


def test_idempotent():
    def before(x):
        return P(P(x))

    _check_opt(before, _idempotent_after,
               idempotent_P)


def test_idempotent_multi():
    def before(x):
        return P(P(P(P(x))))

    _check_opt(before, _idempotent_after,
               idempotent_P)


def test_idempotent_and_elim():
    def before(x):
        return P(R(P(R(R(P(x))))))

    _check_opt(before, _idempotent_after,
               idempotent_P, elim_R)


def test_multiply_zero():
    def before(x):
        return x * 0

    def after(x):
        return 0

    _check_opt(before, after,
               multiply_by_zero_l, multiply_by_zero_r)


def test_multiply_add_elim_zero():
    def before(x, y):
        return x + y * R(0)

    def after(x, y):
        return x

    _check_opt(before, after,
               elim_R, multiply_by_zero_r, add_zero_r)


def test_replace_twice():
    def before(x):
        return Q(0)

    def after(x):
        return 0

    _check_opt(before, after,
               Q0_to_R, elim_R)


def test_revisit():
    def before(x):
        return Q(P(x))

    def after(x):
        return Q(x)

    _check_opt(before, after,
               QP_to_QR, elim_R)


def test_multi_function():
    def before_helper(x, y):
        return R(x) * R(y)

    def before(x):
        return before_helper(R(x), 3)

    def after_helper(x, y):
        return x * y

    def after(x):
        return after_helper(x, 3)

    _check_opt(before, after,
               elim_R)


def test_closure():
    def before(x):
        y = P(x)

        def sub():
            return Q(y)
        return sub()

    def after(x):

        def sub():
            return Q(x)
        return sub()

    _check_opt(before, after,
               QP_to_QR, elim_R)


def test_closure_2():
    def before(x):
        # Note that y is only accessible through the closure
        y = R(x)

        def sub():
            return y
        return sub()

    def after(x):
        def sub():
            return x
        return sub()

    _check_opt(before, after,
               elim_R)


def test_fn_replacement():
    def before(x):
        return Q(P(P(P(P(x)))))

    def after(x):
        return x

    @pattern_replacer(Q, X)
    def elim_QPs(optimizer, node, equiv):
        # Q(P(...P(x))) => x
        arg = equiv[X]
        while arg.inputs and arg.inputs[0].value == P:
            arg = arg.inputs[1]
        return arg

    _check_opt(before, after,
               elim_QPs)


def test_constant_variable():
    def before(x):
        return Q(15) + Q(x)

    def after(x):
        return P(15) + Q(x)

    Qct_to_P = psub(
        (Q, V),
        (P, V),
        name='Qct_to_P'
    )

    _check_opt(before, after,
               Qct_to_P)


def test_cse():

    def helper(fn, before, after):
        gbefore = parse(fn)
        assert len(gbefore.nodes) == before

        gafter = cse(gbefore, gbefore.manager)
        assert len(gafter.nodes) == after

    def f1(x, y):
        a = x + y
        b = x + y
        c = a * b
        return c

    helper(f1, 6, 5)

    def f2(x, y):
        a = x + y
        b = (a * y) + (a / x)
        c = (a * y) + ((x + y) / x)
        d = b + c
        return d

    helper(f2, 12, 8)


opt_ok = psub(
    (prim.scalar_add, X, Y),
    (prim.scalar_mul, X, Y),
    name='opt_ok'
)


opt_err1 = psub(
    (prim.scalar_sub, X, Y),
    (prim.scalar_lt, X, Y),
    name='opt_err1'
)


opt_err2 = psub(
    prim.scalar_div,
    prim.scalar_lt,
    name='opt_err2'
)


def test_type_tracking():

    pip = scalar_pipeline \
        .select('parse', 'infer', 'specialize',
                'prepare', 'opt', 'validate') \
        .configure({
            'opt.opts': [opt_ok, opt_err1, opt_err2],
            'prepare.watch': True
        })

    def fn1(x, y):
        return x + y

    pip.run(input=fn1, argspec=({'type': i64}, {'type': i64}))

    def fn2(x, y):
        return x - y

    with pytest.raises(InferenceError):
        pip.run(input=fn2, argspec=({'type': i64}, {'type': i64}))

    def fn3(x, y):
        return x / y

    with pytest.raises(InferenceError):
        pip.run(input=fn3, argspec=({'type': i64}, {'type': i64}))
