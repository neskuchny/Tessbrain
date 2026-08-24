# -*- coding: utf-8 -*-
"""Safe calc-engine — безопасное вычисление арифметических выражений.

Playbook задаёт `computation` (напр. "debt / equity", "(rev - cost) / rev"),
которое нужно выполнить над извлечёнными из документа числами. Использовать
встроенный eval() НЕЛЬЗЯ (RCE). Здесь — AST-walker, который допускает
ТОЛЬКО:
  - числовые литералы
  - имена переменных (из переданного namespace = inputs)
  - бинарные операции: + - * / // % **
  - унарные: + -
  - сравнения (для будущих benchmark-выражений)
  - whitelisted функции: sum, avg, min, max, abs, round, len

Любой другой узел AST (вызов произвольной функции, атрибут, импорт,
lambda, comprehension) → SafeCalcError. Деление на ноль → None (не падаем).

Это расширяемо до execute_code sandbox (Hermes executors) для сложных
расчётов — отдельный backend, тот же интерфейс evaluate().
"""
from __future__ import annotations

import ast
import operator
from typing import Any, Optional


class SafeCalcError(Exception):
    """Выражение содержит недопустимую конструкцию."""


# Разрешённые бинарные операторы.
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _avg(*args) -> float:
    nums = [float(a) for a in args if a is not None]
    return sum(nums) / len(nums) if nums else 0.0


# Whitelisted функции. Принимают либо varargs, либо один iterable.
def _flex(fn):
    """Разрешить и f(a,b,c), и f([a,b,c])."""
    def _wrap(*args):
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            return fn(*args[0])
        return fn(*args)
    return _wrap


_FUNCS = {
    "sum": lambda *a: sum(a[0]) if len(a) == 1 and isinstance(a[0], (list, tuple)) else sum(a),
    "avg": _flex(_avg),
    "mean": _flex(_avg),
    "min": _flex(min),
    "max": _flex(max),
    "abs": lambda x: abs(x),
    "round": lambda x, n=2: round(x, n),
    "len": lambda x: len(x),
}

# Лимит на размер выражения (защита от ast-бомб).
_MAX_EXPR_LEN = 500


def evaluate(expression: str, namespace: dict[str, Any]) -> Optional[float]:
    """Вычислить выражение над namespace. Возвращает число или None.

    None при: делении на ноль, отсутствии переменной, нечисловом результате.
    Raises SafeCalcError при недопустимой AST-конструкции.
    """
    if not expression or not expression.strip():
        return None
    if len(expression) > _MAX_EXPR_LEN:
        raise SafeCalcError("expression too long")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise SafeCalcError(f"syntax error: {e}")

    try:
        result = _eval_node(tree.body, namespace)
    except ZeroDivisionError:
        return None
    except KeyError:
        return None  # переменная не найдена → не считаем (нет данных)

    if isinstance(result, bool):
        return result  # сравнения возвращают bool — для benchmark-выражений
    if isinstance(result, (int, float)):
        return float(result)
    return None


# Лимит на число строк в multi-step программе (защита от больших скриптов).
_MAX_PROGRAM_LINES = 30


def evaluate_program(code: str, namespace: dict[str, Any]) -> Optional[float]:
    """Вычислить МНОГОШАГОВУЮ формулу с промежуточными переменными.

    Поддерживает несколько строк:
      - присвоение: `name = <выражение>` (кладёт результат в namespace)
      - финальная строка: выражение → его значение возвращается

    Пример (маржа в два шага):
        gross = rev - cost
        margin = gross / rev
        margin

    Безопасность та же что у evaluate(): только арифметика + whitelisted
    функции, никаких импортов/вызовов/атрибутов. Каждая правая часть
    присвоения проходит тот же AST-whitelist.

    None при: делении на ноль, отсутствии переменной, нечисловом результате.
    Raises SafeCalcError при недопустимой конструкции.
    """
    if not code or not code.strip():
        return None
    if len(code) > _MAX_EXPR_LEN * _MAX_PROGRAM_LINES:
        raise SafeCalcError("program too long")

    # Однострочное выражение без присвоений — обычный evaluate.
    if "\n" not in code and "=" not in code.split("==")[0]:
        return evaluate(code, namespace)

    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        raise SafeCalcError(f"syntax error: {e}")

    if len(tree.body) > _MAX_PROGRAM_LINES:
        raise SafeCalcError("too many statements")

    ns = dict(namespace)  # не мутируем входной namespace
    last_value: Any = None

    try:
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                # Только простые присвоения: name = expr (один target).
                if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                    raise SafeCalcError("only simple 'name = expr' assignments allowed")
                val = _eval_node(stmt.value, ns)
                ns[stmt.targets[0].id] = val
                last_value = val
            elif isinstance(stmt, ast.Expr):
                last_value = _eval_node(stmt.value, ns)
            else:
                raise SafeCalcError(
                    f"disallowed statement: {type(stmt).__name__}"
                )
    except ZeroDivisionError:
        return None
    except KeyError:
        return None

    if isinstance(last_value, bool):
        return last_value
    if isinstance(last_value, (int, float)):
        return float(last_value)
    return None


def _eval_node(node: ast.AST, ns: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise SafeCalcError(f"disallowed constant: {node.value!r}")

    if isinstance(node, ast.Name):
        if node.id in ns:
            v = ns[node.id]
            return v
        raise KeyError(node.id)

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise SafeCalcError(f"disallowed operator: {type(node.op).__name__}")
        left = _eval_node(node.left, ns)
        right = _eval_node(node.right, ns)
        return op(left, right)

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise SafeCalcError(f"disallowed unary op: {type(node.op).__name__}")
        return op(_eval_node(node.operand, ns))

    if isinstance(node, ast.Compare):
        # a < b < c → цепочка
        left = _eval_node(node.left, ns)
        result = True
        for op_node, comparator in zip(node.ops, node.comparators, strict=False):
            op = _CMP_OPS.get(type(op_node))
            if op is None:
                raise SafeCalcError(f"disallowed comparison: {type(op_node).__name__}")
            right = _eval_node(comparator, ns)
            result = result and op(left, right)
            left = right
        return result

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise SafeCalcError("only simple function calls allowed")
        fn = _FUNCS.get(node.func.id)
        if fn is None:
            raise SafeCalcError(f"disallowed function: {node.func.id}")
        if node.keywords:
            raise SafeCalcError("keyword args not allowed")
        args = [_eval_node(a, ns) for a in node.args]
        return fn(*args)

    if isinstance(node, ast.List):
        return [_eval_node(e, ns) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(e, ns) for e in node.elts)

    raise SafeCalcError(f"disallowed expression node: {type(node).__name__}")
