# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Cell values of an .xlsx as Excel would show them, for knowledge-work probes.

A workbook written by openpyxl (or any library) has no cached results for its
formulas, so ``load_workbook(data_only=True)`` returns ``None`` for every live
formula. Excel recalculates such a file when it is opened, so the reader sees the
results; probes must too, without penalising an agent for keeping totals live.
A cell without a cached value is evaluated here, the way Excel would:

- references: ``A1``, ``$A$1``, ``Sheet!A1``, ``'My Sheet'!A1``, ranges ``A1:B9``
  and whole columns ``D:D`` (the sheet's used rows);
- numbers, ``"text"``, ``TRUE``/``FALSE``; ``+ - * / ^``, unary minus, ``&``
  and the comparisons ``= <> < > <= >=``;
- SUM, AVERAGE, MIN, MAX, ROUND, ABS, COUNT, COUNTA, IF, IFS, IFERROR, IFNA, ISERROR,
  SUMIF, SUMIFS, COUNTIF, COUNTIFS, AVERAGEIF, AVERAGEIFS, SUBTOTAL, SUMPRODUCT, AND, OR,
  NOT, ISNUMBER, ISBLANK;
- text that Excel reads as a number in arithmetic ("$200,000", "1,500", "12%")
  counts as that number, as it does in Excel.

Nothing is passed to ``eval``: formulas are tokenised and parsed by a small
recursive-descent parser. Anything else raises :class:`FormulaError` naming it,
so a probe fails loudly instead of reading the cell as blank. Excel error values
(division by zero, arithmetic on text) are :class:`ExcelError`, which IFERROR
catches and which otherwise fail like any other FormulaError.

``build_fixtures.py`` stages this file as ``_xlsx_eval.py`` in the keys
directory (``$KW_KEYS``), where probes import it.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, List, Optional, Tuple

FUNCTIONS = ("SUM", "AVERAGE", "MIN", "MAX", "ROUND", "ABS", "COUNT", "COUNTA", "IF", "IFS", "IFERROR", "IFNA",
             "ISERROR", "SUMIF", "SUMIFS", "COUNTIF", "COUNTIFS", "AVERAGEIF", "AVERAGEIFS", "SUBTOTAL",
             "SUMPRODUCT", "AND", "OR", "NOT", "ISNUMBER", "ISBLANK")
#: SUBTOTAL's function numbers (101-111: the same, ignoring hidden rows, which a file read can't see).
SUBTOTAL = {1: "AVERAGE", 2: "COUNT", 3: "COUNTA", 4: "MAX", 5: "MIN", 9: "SUM"}


class FormulaError(ValueError):
    """A formula this evaluator cannot compute, with the cell and the reason."""


class ExcelError(FormulaError):
    """A formula Excel itself would show as an error (#DIV/0!, #VALUE!); IFERROR catches it."""


_TOKEN = re.compile(
    r"""\s*(?:
    (?P<str>"(?:[^"]|"")*") |
    (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?%?|\.\d+%?) |
    (?P<ref>(?:(?:'(?P<qs>[^']+)'|(?P<us>[A-Za-z_][\w.]*))!)?
        (?:\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?|\$?[A-Za-z]{1,3}:\$?[A-Za-z]{1,3}))(?![\w(]) |
    (?P<fn>[A-Za-z_][A-Za-z0-9_.]*)\s*\( |
    (?P<bool>TRUE|FALSE)(?![\w(]) |
    (?P<op><>|<=|>=|[-+*/^(),&=<>])
    )""",
    re.X | re.I,
)
_CELL = re.compile(r"\$?([A-Za-z]{1,3})\$?(\d+)")
_COLUMN = re.compile(r"\$?([A-Za-z]{1,3})")
#: Text Excel converts to a number in arithmetic: optional sign, $, thousands commas, %, (negative).
_NUMERIC_TEXT = re.compile(r"\s*(\()?\s*([-+])?\s*\$?\s*(\d{1,3}(?:,\d{3})+|\d+)?(\.\d+)?\s*(%)?\s*(\))?\s*")
_COMPARE = ("=", "<>", "<", ">", "<=", ">=")


def _col(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n


def _coord(text: str) -> Tuple[int, int]:
    m = _CELL.fullmatch(text)
    return int(m.group(2)), _col(m.group(1))


def excel_serial(v: Any) -> Optional[float]:
    """A date or datetime as Excel's serial number (days since 1899-12-30), as Excel computes with it."""
    import datetime as _dt

    if isinstance(v, _dt.datetime):
        return (v - _dt.datetime(1899, 12, 30)).total_seconds() / 86400
    if isinstance(v, _dt.date):
        return float((v - _dt.date(1899, 12, 30)).days)
    return None


def number_from_text(text: str) -> Optional[float]:
    """The number Excel reads in *text* ("$1,234.50", "(200)", "12%"), or None."""
    m = _NUMERIC_TEXT.fullmatch(text)
    if not m or not (m.group(3) or m.group(4)) or bool(m.group(1)) != bool(m.group(6)):
        return None
    v = float((m.group(3) or "0").replace(",", "") + (m.group(4) or ""))
    if m.group(5):
        v /= 100
    return -v if (m.group(1) or m.group(2) == "-") else v


class Workbook:
    """Both views of one workbook; ``value(sheet, row, col)`` gives the shown value."""

    def __init__(self, path: Any):
        from openpyxl import load_workbook

        self._f = load_workbook(path)
        self._v = load_workbook(path, data_only=True)
        self._memo: Dict[Tuple[str, int, int], Any] = {}
        self._busy: set = set()

    @property
    def sheetnames(self) -> List[str]:
        return self._f.sheetnames

    def max_row(self, sheet: str) -> int:
        if sheet not in self._f.sheetnames:
            raise FormulaError(f"reference to missing sheet {sheet!r}")
        return self._f[sheet].max_row

    def value(self, sheet: str, row: int, col: int) -> Any:
        key = (sheet, row, col)
        if key in self._memo:
            return self._memo[key]
        if sheet not in self._f.sheetnames:
            raise FormulaError(f"reference to missing sheet {sheet!r}")
        cached = self._v[sheet].cell(row=row, column=col).value
        raw = self._f[sheet].cell(row=row, column=col).value
        if cached is not None or not (isinstance(raw, str) and raw.startswith("=")):
            if raw is not None and not isinstance(raw, (str, int, float, bool)) and cached is None:
                if hasattr(raw, "date"):  # a date or datetime cell: a value, shown as such
                    result = raw
                else:
                    raise FormulaError(f"{sheet}!{self._name(row, col)}: unsupported formula type {type(raw).__name__}")
            else:
                result = cached if cached is not None else raw
        else:
            if key in self._busy:
                raise FormulaError(f"{sheet}!{self._name(row, col)}: circular reference")
            self._busy.add(key)
            try:
                result = _Parser(self, sheet, raw[1:], f"{sheet}!{self._name(row, col)}").parse()
            finally:
                self._busy.discard(key)
        self._memo[key] = result
        return result

    @staticmethod
    def _name(row: int, col: int) -> str:
        letters = ""
        while col:
            col, rem = divmod(col - 1, 26)
            letters = chr(65 + rem) + letters
        return f"{letters}{row}"

    def rows(self) -> List[Tuple[str, List[List[Any]]]]:
        """Every sheet as rows of shown values (formulas evaluated where not cached)."""
        out = []
        for name in self._f.sheetnames:
            ws = self._f[name]
            rows = []
            for r in range(1, ws.max_row + 1):
                rows.append([self.value(name, r, c) for c in range(1, ws.max_column + 1)])
            out.append((name, rows))
        return out


class _Range:
    """Cells of a range argument, in row order."""

    def __init__(self, values: List[Any]):
        self.values = values


class _Parser:
    def __init__(self, wb: Workbook, sheet: str, text: str, where: str):
        self.wb, self.sheet, self.where = wb, sheet, where
        self.toks: List[Tuple[str, Any]] = []
        pos = 0
        text = text.strip()
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if not m or m.end() == pos:
                raise FormulaError(f"{where}: cannot evaluate '={text}' (unsupported syntax near {text[pos:pos + 12]!r})")
            pos = m.end()
            if m.group("str") is not None:
                self.toks.append(("str", m.group("str")[1:-1].replace('""', '"')))
            elif m.group("num"):
                s = m.group("num")
                self.toks.append(("num", float(s[:-1]) / 100 if s.endswith("%") else float(s)))
            elif m.group("ref"):
                sh = m.group("qs") or m.group("us") or sheet
                cells = m.group("ref").split("!")[-1]
                self.toks.append(("ref", (sh, cells)))
            elif m.group("fn"):
                name = m.group("fn").upper()
                if name not in FUNCTIONS:
                    raise FormulaError(f"{where}: unsupported function {name}() in '={text}'")
                self.toks.append(("fn", name))
            elif m.group("bool"):
                self.toks.append(("bool", m.group("bool").upper() == "TRUE"))
            else:
                self.toks.append(("op", m.group("op")))
        self.i = 0

    # ---- tokens ----------------------------------------------------------
    def _peek(self) -> Optional[Tuple[str, Any]]:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _take(self) -> Tuple[str, Any]:
        tok = self._peek()
        if tok is None:
            raise FormulaError(f"{self.where}: formula ends unexpectedly")
        self.i += 1
        return tok

    def _is_op(self, *ops: str) -> bool:
        tok = self._peek()
        return tok is not None and tok[0] == "op" and tok[1] in ops

    # ---- values ----------------------------------------------------------
    def _num(self, v: Any) -> float:
        if v is None:
            return 0.0
        if isinstance(v, bool):
            return float(v)
        if isinstance(v, (int, float)):
            return float(v)
        if excel_serial(v) is not None:
            return excel_serial(v)
        if isinstance(v, str):
            n = number_from_text(v)
            if n is not None:
                return n
        raise ExcelError(f"{self.where}: arithmetic on non-number {v!r}")

    def _text(self, v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)

    def _bool(self, v: Any) -> bool:
        if isinstance(v, str):
            if v.upper() in ("TRUE", "FALSE"):
                return v.upper() == "TRUE"
            raise ExcelError(f"{self.where}: {v!r} is not TRUE or FALSE")
        return bool(self._num(v))

    def _cells(self, ref: Tuple[str, str]) -> List[Any]:
        sheet, cells = ref
        if ":" in cells:
            a, b = cells.split(":")
            if _COLUMN.fullmatch(a) and _COLUMN.fullmatch(b):  # whole columns: the sheet's used rows
                last = self.wb.max_row(sheet)
                c1, c2 = _col(_COLUMN.fullmatch(a).group(1)), _col(_COLUMN.fullmatch(b).group(1))
                return [self.wb.value(sheet, r, c) for r in range(1, last + 1)
                        for c in range(min(c1, c2), max(c1, c2) + 1)]
            (r1, c1), (r2, c2) = _coord(a), _coord(b)
            return [self.wb.value(sheet, r, c) for r in range(min(r1, r2), max(r1, r2) + 1)
                    for c in range(min(c1, c2), max(c1, c2) + 1)]
        r, c = _coord(cells)
        return [self.wb.value(sheet, r, c)]

    # ---- grammar (Excel precedence, lowest first) -------------------------
    def parse(self) -> Any:
        v = self._compare()
        if self._peek() is not None:
            raise FormulaError(f"{self.where}: unexpected {self._peek()[1]!r}")
        if isinstance(v, _Range):
            raise FormulaError(f"{self.where}: a range is only valid inside a function")
        return v

    def _compare(self) -> Any:
        v = self._concat()
        while self._is_op(*_COMPARE):
            op = self._take()[1]
            v = _compare(v, op, self._concat())
        return v

    def _concat(self) -> Any:
        v = self._expr()
        while self._is_op("&"):
            self._take()
            v = self._text(self._scalar(v)) + self._text(self._scalar(self._expr()))
        return v

    def _expr(self) -> Any:
        v = self._term()
        while self._is_op("+", "-"):
            op = self._take()[1]
            a, b = self._num(self._scalar(v)), self._num(self._scalar(self._term()))
            v = a + b if op == "+" else a - b
        return v

    def _term(self) -> Any:
        v = self._power()
        while self._is_op("*", "/"):
            op = self._take()[1]
            a, b = self._num(self._scalar(v)), self._num(self._scalar(self._power()))
            if op == "/" and b == 0:
                raise ExcelError(f"{self.where}: division by zero")
            v = a * b if op == "*" else a / b
        return v

    def _power(self) -> Any:
        v = self._unary()
        if self._is_op("^"):
            self._take()
            v = self._num(self._scalar(v)) ** self._num(self._scalar(self._unary()))
        return v

    def _unary(self) -> Any:
        if self._is_op("-"):
            self._take()
            return -self._num(self._scalar(self._unary()))
        if self._is_op("+"):
            self._take()
            return self._unary()
        return self._primary()

    def _scalar(self, v: Any) -> Any:
        if isinstance(v, _Range):
            raise FormulaError(f"{self.where}: a range is only valid inside a function")
        return v

    def _primary(self) -> Any:
        kind, val = self._take()
        if kind in ("num", "str", "bool"):
            return val
        if kind == "ref":
            values = self._cells(val)
            return _Range(values) if ":" in val[1] else values[0]
        if kind == "fn":
            return self._call(val)
        if (kind, val) == ("op", "("):
            v = self._compare()
            if self._take() != ("op", ")"):
                raise FormulaError(f"{self.where}: missing ')'")
            return v
        raise FormulaError(f"{self.where}: unexpected {val!r}")

    # ---- functions --------------------------------------------------------
    def _arg(self) -> Any:
        """One argument: a range stays a range; IFERROR's first argument may be an Excel error."""
        return self._compare()

    def _lazy(self) -> Any:
        """The next argument, or _Untaken() if it is an Excel error: only an argument used may fail."""
        start = self.i
        try:
            return self._scalar(self._arg())
        except ExcelError:
            self.i = start
            self._skip_argument()
            return _Untaken()

    def _call(self, name: str) -> Any:
        if name == "ISERROR":
            value = self._lazy()
            self._close(name)
            return isinstance(value, _Untaken)
        if name == "IFNA":
            # Nothing here yields #N/A (no lookups), so the value stands; any other error still shows.
            value = self._scalar(self._arg())
            if not self._is_op(","):
                raise FormulaError(f"{self.where}: IFNA needs two arguments")
            self._take()
            self._lazy()
            self._close(name)
            return value
        if name == "IFS":
            chosen: Any = None
            found = False
            while True:  # conditions in order; once one is true, the rest are skipped, not evaluated
                if found:
                    self._skip_argument()
                    hit = False
                else:
                    found = hit = self._bool(self._scalar(self._arg()))
                if not self._is_op(","):
                    raise FormulaError(f"{self.where}: IFS needs condition, value pairs")
                self._take()
                value = self._lazy()
                if hit:
                    chosen = value
                if not self._is_op(","):
                    break
                self._take()
            self._close(name)
            if not found:
                raise ExcelError(f"{self.where}: no IFS condition is true (#N/A)")
            if isinstance(chosen, _Untaken):
                raise ExcelError(f"{self.where}: IFS chose a value that is an error")
            return chosen
        if name == "IFERROR":
            start = self.i
            try:
                value = self._scalar(self._arg())
                failed = False
            except ExcelError:
                failed, value = True, None
                self.i = start
                self._skip_argument()
            if not self._is_op(","):
                raise FormulaError(f"{self.where}: IFERROR needs two arguments")
            self._take()
            fallback = self._scalar(self._arg())
            self._close(name)
            return fallback if failed else value
        if name == "IF":
            cond = self._bool(self._scalar(self._arg()))
            args = []
            while self._is_op(","):
                self._take()
                start = self.i
                try:
                    args.append(self._scalar(self._arg()))
                except ExcelError:
                    # Excel only evaluates the branch it takes; an error in the other one doesn't count.
                    self.i = start
                    self._skip_argument()
                    args.append(_Untaken())
            self._close(name)
            if not args or len(args) > 2:
                raise FormulaError(f"{self.where}: IF needs two or three arguments")
            chosen = args[0] if cond else (args[1] if len(args) == 2 else False)
            if isinstance(chosen, _Untaken):
                raise ExcelError(f"{self.where}: IF chose a branch that is an error")
            return chosen
        args: List[Any] = []
        if not self._is_op(")"):
            while True:
                args.append(self._arg())
                if self._is_op(","):
                    self._take()
                    continue
                break
        self._close(name)
        return self._apply(name, args)

    def _close(self, name: str) -> None:
        if self._take() != ("op", ")"):
            raise FormulaError(f"{self.where}: missing ')' after {name}(")

    def _skip_argument(self) -> None:
        depth = 0
        while self._peek() is not None:
            tok = self._peek()
            if tok[0] in ("fn",) or tok == ("op", "("):
                depth += 1
            elif tok == ("op", ")"):
                if depth == 0:
                    return
                depth -= 1
            elif tok == ("op", ",") and depth == 0:
                return
            self.i += 1

    def _numbers(self, args: List[Any]) -> List[float]:
        """SUM-style: numbers in ranges (text and logicals there are ignored); scalars coerced."""
        out: List[float] = []
        for a in args:
            if isinstance(a, _Range):
                out += [float(v) if excel_serial(v) is None else excel_serial(v) for v in a.values
                        if (isinstance(v, (int, float)) and not isinstance(v, bool)) or excel_serial(v) is not None]
            else:
                out.append(self._num(a))
        return out

    def _apply(self, name: str, args: List[Any]) -> Any:
        if name == "SUM":
            return sum(self._numbers(args))
        if name == "COUNT":
            return float(len(self._numbers([a if isinstance(a, _Range) else _Range([a]) for a in args])))
        if name == "COUNTA":
            vals = [v for a in args for v in (a.values if isinstance(a, _Range) else [a])]
            return float(sum(1 for v in vals if v not in (None, "")))
        if name == "AVERAGE":
            flat = self._numbers(args)
            if not flat:
                raise ExcelError(f"{self.where}: AVERAGE of no numbers")
            return sum(flat) / len(flat)
        if name in ("MIN", "MAX"):
            flat = self._numbers(args)
            return (min if name == "MIN" else max)(flat) if flat else 0.0
        if name == "ABS":
            return abs(self._num(self._scalar(args[0])))
        if name == "ROUND":
            if len(args) != 2:
                raise FormulaError(f"{self.where}: ROUND needs two arguments")
            x, n = self._num(self._scalar(args[0])), int(self._num(self._scalar(args[1])))
            q = 10 ** n
            return float(int(abs(x) * q + 0.5)) / q * (1 if x >= 0 else -1)  # Excel rounds half away from zero
        if name in ("AND", "OR"):
            vals = [self._bool(v) for a in args for v in (a.values if isinstance(a, _Range) else [a]) if v is not None]
            return all(vals) if name == "AND" else any(vals)
        if name == "NOT":
            return not self._bool(self._scalar(args[0]))
        if name == "ISNUMBER":
            v = self._scalar(args[0])
            return isinstance(v, (int, float)) and not isinstance(v, bool)
        if name == "ISBLANK":
            return self._scalar(args[0]) is None
        if name in ("SUMIF", "COUNTIF"):
            if not isinstance(args[0], _Range) and name == "SUMIF" and len(args) < 3:
                args[0] = _Range([args[0]])
            rng = args[0].values if isinstance(args[0], _Range) else [args[0]]
            test = _criterion(self._scalar(args[1]))
            if name == "COUNTIF":
                return float(sum(1 for v in rng if test(v)))
            sums = args[2].values if len(args) > 2 and isinstance(args[2], _Range) else rng
            return float(sum(s for v, s in zip(rng, sums)
                             if test(v) and isinstance(s, (int, float)) and not isinstance(s, bool)))
        if name in ("SUMIFS", "COUNTIFS", "AVERAGEIFS"):
            first = 0 if name == "COUNTIFS" else 1
            if len(args) < first + 2 or (len(args) - first) % 2:
                raise FormulaError(f"{self.where}: {name} needs range, criterion pairs")
            pairs = [(args[i], _criterion(self._scalar(args[i + 1]))) for i in range(first, len(args), 2)]
            ranges = [r.values if isinstance(r, _Range) else [r] for r, _ in pairs]
            target = (args[0].values if isinstance(args[0], _Range) else [args[0]]) if first else ranges[0]
            if len({len(r) for r in ranges + [target]}) != 1:
                raise ExcelError(f"{self.where}: {name} ranges differ in size")
            rows = [i for i in range(len(target)) if all(test(r[i]) for r, (_, test) in zip(ranges, pairs))]
            if name == "COUNTIFS":
                return float(len(rows))
            nums = [target[i] for i in rows if isinstance(target[i], (int, float)) and not isinstance(target[i], bool)]
            if name == "SUMIFS":
                return float(sum(nums))
            if not nums:
                raise ExcelError(f"{self.where}: AVERAGEIFS of no numbers")
            return sum(nums) / len(nums)
        if name == "AVERAGEIF":
            rng = args[0].values if isinstance(args[0], _Range) else [args[0]]
            test = _criterion(self._scalar(args[1]))
            avg = args[2].values if len(args) > 2 and isinstance(args[2], _Range) else rng
            nums = [s for v, s in zip(rng, avg) if test(v) and isinstance(s, (int, float)) and not isinstance(s, bool)]
            if not nums:
                raise ExcelError(f"{self.where}: AVERAGEIF of no numbers")
            return sum(nums) / len(nums)
        if name == "SUBTOTAL":
            code = int(self._num(self._scalar(args[0]))) if args else 0
            inner = SUBTOTAL.get(code % 100 if code > 100 else code)
            if inner is None:
                raise FormulaError(f"{self.where}: unsupported SUBTOTAL function {code}")
            return self._apply(inner, args[1:])
        if name == "SUMPRODUCT":
            cols = [a.values if isinstance(a, _Range) else [a] for a in args]
            if len({len(c) for c in cols}) != 1:
                raise ExcelError(f"{self.where}: SUMPRODUCT ranges differ in size")
            total = 0.0
            for row in zip(*cols):
                prod = 1.0
                for v in row:
                    prod *= float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
                total += prod
            return total
        raise FormulaError(f"{self.where}: unsupported function {name}()")


class _Untaken:
    """An IF branch that would be an Excel error; harmless unless IF chooses it."""


def _compare(a: Any, op: str, b: Any) -> bool:
    """Excel comparison: numbers with numbers, text case-insensitively, blank as 0 or ""."""
    def norm(v: Any, other: Any) -> Any:
        if v is None:
            return "" if isinstance(other, str) else 0.0
        if isinstance(v, bool):
            return float(v)
        if excel_serial(v) is not None:
            return excel_serial(v)
        return v.lower() if isinstance(v, str) else float(v)
    x, y = norm(a, b), norm(b, a)
    if isinstance(x, str) != isinstance(y, str):  # Excel orders numbers before text
        x, y = (0, 1) if not isinstance(x, str) else (1, 0)
    return {"=": x == y, "<>": x != y, "<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y}[op]


def _criterion(crit: Any):
    """SUMIF/COUNTIF criteria: 5, ">0", "<>C", "=Atlas", "Beac*" (wildcards), case-insensitive."""
    if not isinstance(crit, str):
        return lambda v: v is not None and not isinstance(v, str) and _compare(v, "=", crit)
    m = re.match(r"(<>|<=|>=|=|<|>)?(.*)", crit, re.S)
    op, rhs = m.group(1) or "=", m.group(2)
    n = number_from_text(rhs) if rhs.strip() else None
    if n is not None:
        def num_test(v: Any) -> bool:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return _compare(v, op, n)
            return op == "<>"
        return num_test
    if op in ("=", "<>") and any(ch in rhs for ch in "*?"):
        def wild(v: Any) -> bool:
            hit = fnmatch.fnmatchcase(("" if v is None else str(v)).lower(), rhs.lower())
            return hit if op == "=" else not hit
        return wild
    return lambda v: _compare("" if v is None else (v if isinstance(v, str) else str(v)), op, rhs)


def workbook_rows(path: Any) -> List[Tuple[str, List[List[Any]]]]:
    """``[(sheet name, rows of shown values)]`` for the workbook at *path*."""
    return Workbook(path).rows()
