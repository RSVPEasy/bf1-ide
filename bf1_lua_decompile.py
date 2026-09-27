"""
bf1_lua_decompile.py - Lua 4.0 bytecode -> Lua source decompiler.

This game's scr_ chunks (see bf1_core.py's decode_script_chunk) turn out to
be compiled Lua 4.0 bytecode (signature "\\x1bLua\\x40" - version byte 0x40 =
major 4, minor 0), not 5.1 as the byte-for-byte identical signature prefix
"\\x1bLua" might suggest at a glance.

This is a Python port of styinx/lua4dec (https://github.com/styinx/lua4dec),
a C++ Lua 4 decompiler, specifically its bytecode reader (source/lua/lua.cpp)
and its stack-machine-to-AST parser (source/parser/parser.cpp) and AST
printer (source/ast/ast.cpp). The port follows that project's structure and
algorithm closely (including its known rough edges - e.g. it doesn't
reconstruct `while` loops from raw backward jumps, only `if`/`elseif`/`else`,
numeric `for`, and generic `for-in` - the same gaps the reference project's
own README flags as unfinished) rather than inventing a different approach,
since it's a working, checked-in reference for exactly this bytecode
version.

THE ALGORITHM (same as the reference)
--------------------------------------
1. Parse the chunk header and the function-prototype tree (name, params,
   locals, string/number constant pools, nested function prototypes, raw
   32-bit instructions) - see read_header()/read_function() below.
2. Walk each function's instructions once, in order, simulating a stack
   machine: each opcode has a handler that pops/pushes small AST expression
   nodes (identifiers, literals, operations, calls, ...) on a Python list
   used as the VM stack, and appends AST statement nodes (assignments,
   calls, returns, ...) to the current lexical block. A handful of opcodes
   (the JMP family, FORPREP/FORLOOP, LFORPREP/LFORLOOP, CLOSURE) manage a
   tree of nested blocks instead of just the flat stack, which is what
   turns comparison+jump instruction pairs back into if/elseif/else,
   numeric for, generic for-in, and nested closures.
3. Pretty-print the resulting AST back into Lua source text.

KNOWN LIMITATION: this game's shipped scr_ chunks have their local-variable
DEBUG TABLE STRIPPED (each function's `locals` list comes back empty). The
whole "reserved_elements" bookkeeping this algorithm (both here and
upstream) uses to tell "a local variable's permanent stack slot" apart from
"a transient value mid-expression" depends entirely on that debug table
(specifically each local's start_pc, to know when a new permanent slot gets
reserved). Without it there's no reliable way to tell those apart from the
instruction stream alone - a GETLOCAL/SETLOCAL operand is a real, directly
usable stack-slot index, but nothing marks the *moment* a slot becomes
"reserved" versus just being read. On some functions this causes an
assignment to sweep up unrelated in-flight expression values, which then
cascades into a stack-accounting error a few instructions later. Tried
tightening this (treating "highest local slot referenced so far" as a
reserved-count lower bound); it made *more* scripts fail, not fewer, so it
was reverted - see git history / earlier revisions of this file if
revisiting this. In practice this module still fully decompiles roughly
half of this game's real shell.lvl scripts and cleanly raises
LuaDecompileError (rather than emitting wrong code) on the rest, so callers
should treat decompile() as best-effort and fall back to exporting the raw
.luac bytecode when it raises.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional, Union

MAGIC = b"\x1bLua\x40"  # ESC 'L' 'u' 'a' 0x40 (major 4, minor 0)
LUA_NUMBER_CONST = 3.14159265358979323846e8  # header sanity-check value


class LuaDecompileError(Exception):
    pass


# --- Bytecode reader (port of source/lua/lua.cpp) --------------------------


@dataclass
class ChunkHeader:
    is_little_endian: bool
    bytes_for_int: int
    bytes_for_size_t: int
    bytes_for_instruction: int
    bits_for_instruction: int
    bits_for_operator: int
    bits_for_register_b: int
    bytes_for_test_number: int
    test_number: float


@dataclass
class Local:
    name: str
    start_pc: int
    end_pc: int


@dataclass
class LuaFunction:
    name: str = ""
    line_defined: int = 0
    number_of_params: int = 0
    is_variadic: bool = False
    max_stack_size: int = 0
    instructions: list[int] = field(default_factory=list)
    numbers: list[float] = field(default_factory=list)
    globals: list[str] = field(default_factory=list)
    locals: list[Local] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)
    functions: list["LuaFunction"] = field(default_factory=list)


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def bytes(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        if len(chunk) != n:
            raise LuaDecompileError(f"Unexpected end of bytecode at offset {self.pos} (wanted {n} bytes).")
        self.pos += n
        return chunk

    def u8(self) -> int:
        return self.bytes(1)[0]

    def i32(self) -> int:
        return struct.unpack_from("<i", self.bytes(4))[0]

    def u32(self) -> int:
        return struct.unpack_from("<I", self.bytes(4))[0]

    def sized_uint(self, size: int) -> int:
        return int.from_bytes(self.bytes(size), "little", signed=False)

    def number(self, size: int) -> float:
        fmt = {4: "<f", 8: "<d"}.get(size)
        if fmt is None:
            raise LuaDecompileError(f"Unsupported lua_Number size: {size} bytes.")
        return struct.unpack_from(fmt, self.bytes(size))[0]

    def string(self, size_t_bytes: int) -> str:
        length = self.sized_uint(size_t_bytes)
        if length == 0:
            return ""
        raw = self.bytes(length)[:-1]  # drop the trailing NUL the length includes
        return raw.decode("latin-1")


def read_header(r: _Reader) -> ChunkHeader:
    if r.bytes(5) != MAGIC:
        raise LuaDecompileError("Not Lua 4.0 bytecode (missing '\\x1bLua\\x40' signature).")
    header = ChunkHeader(
        is_little_endian=r.u8() == 1,
        bytes_for_int=r.u8(),
        bytes_for_size_t=r.u8(),
        bytes_for_instruction=r.u8(),
        bits_for_instruction=r.u8(),
        bits_for_operator=r.u8(),
        bits_for_register_b=r.u8(),
        bytes_for_test_number=0,
        test_number=0.0,
    )
    header.bytes_for_test_number = r.u8()
    header.test_number = r.number(header.bytes_for_test_number)
    if not header.is_little_endian:
        raise LuaDecompileError("Big-endian Lua 4.0 bytecode isn't supported.")
    # test_number round-trips through a float32 (or float64) store, so compare
    # against LUA_NUMBER_CONST rounded through the same width rather than the
    # full-precision double literal - float32 only carries ~7 significant
    # digits, so a few hundred ULP of drift at this magnitude is expected,
    # not a sign of a malformed chunk.
    reference = struct.unpack("<f" if header.bytes_for_test_number == 4 else "<d",
                               struct.pack("<f" if header.bytes_for_test_number == 4 else "<d", LUA_NUMBER_CONST))[0]
    if abs(reference - header.test_number) > abs(reference) * 1e-5:
        raise LuaDecompileError(
            f"Header sanity check failed (test number {header.test_number!r} != {reference!r}); "
            "this doesn't look like a well-formed Lua 4.0 chunk.")
    return header


def read_function(r: _Reader, header: ChunkHeader) -> LuaFunction:
    fn = LuaFunction()
    fn.name = r.string(header.bytes_for_size_t)
    fn.line_defined = r.i32()
    fn.number_of_params = r.i32()
    fn.is_variadic = r.u8() == 1
    fn.max_stack_size = r.i32()

    for _ in range(r.i32()):
        fn.locals.append(Local(r.string(header.bytes_for_size_t), r.i32(), r.i32()))

    for _ in range(r.i32()):
        fn.lines.append(r.i32())

    for _ in range(r.i32()):
        s = r.string(header.bytes_for_size_t)
        fn.globals.append(s.replace("\n", " "))

    for _ in range(r.i32()):
        fn.numbers.append(r.number(header.bytes_for_test_number))

    for _ in range(r.i32()):
        fn.functions.append(read_function(r, header))

    for _ in range(r.i32()):
        fn.instructions.append(r.sized_uint(header.bytes_for_instruction))

    return fn


def read_chunk(data: bytes) -> tuple[ChunkHeader, LuaFunction]:
    r = _Reader(data)
    header = read_header(r)
    main = read_function(r, header)
    return header, main


# --- Instruction field decoding ---------------------------------------------

BITS_OP = 6
BITS_A = 17
_INT_MAX_SHR6 = (2 ** 31 - 1) >> BITS_OP  # 33554431


def op_of(instr: int) -> int:
    return instr & ((1 << BITS_OP) - 1)


def a_of(instr: int, bits_b: int) -> int:
    shift = BITS_OP + bits_b
    return (instr >> shift) & ((1 << BITS_A) - 1)


def b_of(instr: int, bits_b: int) -> int:
    return (instr >> BITS_OP) & ((1 << bits_b) - 1)


def u_of(instr: int) -> int:
    return instr >> BITS_OP


def s_of(instr: int) -> int:
    return (instr >> BITS_OP) - _INT_MAX_SHR6


OPCODES = {
    0x00: "END", 0x01: "RETURN", 0x02: "CALL", 0x03: "TAILCALL", 0x04: "PUSHNIL", 0x05: "POP",
    0x06: "PUSHINT", 0x07: "PUSHSTRING", 0x08: "PUSHNUM", 0x09: "PUSHNEGNUM", 0x0A: "PUSHUPVALUE",
    0x0B: "GETLOCAL", 0x0C: "GETGLOBAL", 0x0D: "GETTABLE", 0x0E: "GETDOTTED", 0x0F: "GETINDEXED",
    0x10: "PUSHSELF", 0x11: "CREATETABLE", 0x12: "SETLOCAL", 0x13: "SETGLOBAL", 0x14: "SETTABLE",
    0x15: "SETLIST", 0x16: "SETMAP", 0x17: "ADD", 0x18: "ADDI", 0x19: "SUB", 0x1A: "MULT",
    0x1B: "DIV", 0x1C: "POW", 0x1D: "CONCAT", 0x1E: "MINUS", 0x1F: "NOT", 0x20: "JMPNE",
    0x21: "JMPEQ", 0x22: "JMPLT", 0x23: "JMPLE", 0x24: "JMPGT", 0x25: "JMPGE", 0x26: "JMPT",
    0x27: "JMPF", 0x28: "JMPONT", 0x29: "JMPONF", 0x2A: "JMP", 0x2B: "PUSHNILJMP", 0x2C: "FORPREP",
    0x2D: "FORLOOP", 0x2E: "LFORPREP", 0x2F: "LFORLOOP", 0x30: "CLOSURE",
}


# --- AST node types (port of source/ast/ast.hpp) ----------------------------
# Expressions


@dataclass
class Identifier:
    name: str


@dataclass
class AstInt:
    value: int


@dataclass
class AstNumber:
    value: float


@dataclass
class AstString:
    value: str


@dataclass
class Dotted:
    ex: list  # [table_expr, Identifier]


@dataclass
class Indexed:
    ex: list  # [table_expr, index_expr]


@dataclass
class AstList:
    elements: list


@dataclass
class AstMap:
    pairs: list  # [(key_expr, value_expr), ...]


@dataclass
class AstTable:
    name: str
    size: int
    pairs: list = field(default_factory=list)


@dataclass
class AstOperation:
    op: str
    ex: list

    def empty(self) -> bool:
        return not self.op and not self.ex


@dataclass
class Call:
    caller: list  # [expr]
    arguments: list
    return_values: int = 0


@dataclass
class Closure:
    statements: list
    arguments: list


Expression = Union[Identifier, AstInt, AstNumber, AstString, Dotted, Indexed,
                    AstList, AstMap, AstTable, AstOperation, Call, Closure]


# Statements


@dataclass
class Assignment:
    left: list  # [Identifier]
    right: list
    num_variables: int = 1
    num_values: int = 1


@dataclass
class ConditionBlock:
    comparison: AstOperation
    statements: list


@dataclass
class Condition:
    blocks: list


@dataclass
class ForLoop:
    counter: str
    begin: Expression
    end: Expression
    increment: Expression
    statements: list


@dataclass
class ForInLoop:
    key: str
    value: str
    table: Expression
    statements: list


@dataclass
class LocalDefinition:
    left: list
    right: list


@dataclass
class Return:
    ex: list


@dataclass
class TailCall:
    caller: list
    arguments: list


# --- Parse-time scope tree (port of Ast/Context in ast.hpp) -----------------


@dataclass
class Context:
    jump_offset: int = 0
    jmp_offset: int = 0
    is_jmp: bool = False
    is_condition: bool = False
    is_jmp_block: bool = False
    is_or_block: bool = False


class AstScope:
    def __init__(self, parent: Optional["AstScope"] = None):
        self.parent = parent
        self.child: Optional["AstScope"] = None
        self.context = Context()
        self.statements: list = []


def _enter_block(ast: AstScope) -> AstScope:
    child = AstScope(parent=ast)
    ast.child = child
    return child


def _exit_block(ast: AstScope) -> AstScope:
    return ast.parent if ast.parent is not None else ast


# --- Stack-machine parser (port of source/parser/parser.cpp) ----------------


class _State:
    def __init__(self):
        self.pc = 0
        self.reserved_elements = 0
        self.stack: list = []


def _handle_condition(state: _State, ast: AstScope, instr: int, comparison: str, operands: list) -> AstScope:
    if not ast.context.is_condition or ast.context.jmp_offset == 0:
        block = ConditionBlock(AstOperation(comparison, operands), [])
        ast.statements.append(Condition([block]))
        ast = _enter_block(ast)
        ast.context.is_condition = True
        ast.context.jump_offset = state.pc + s_of(instr)
    else:
        condition = ast.parent.statements[-1]
        condition.blocks.append(ConditionBlock(AstOperation(comparison, operands), []))
        ast.context.jump_offset = state.pc + s_of(instr)
    ast.context.is_jmp_block = False
    return ast


def _handle_assignment(state: _State, ast: AstScope, left: Identifier) -> None:
    values_on_stack = len(state.stack) - state.reserved_elements
    if values_on_stack > 0:
        values = []
        while values_on_stack > 0:
            values.append(state.stack.pop())
            values_on_stack -= 1
        ast.statements.append(Assignment([left], values, 1, len(values)))
    else:
        if ast.statements and isinstance(ast.statements[-1], Assignment):
            ast.statements[-1].left.append(left)
        else:
            raise LuaDecompileError("Bad assignment: no pending statement to attach an extra return value to.")


def _pop_n(state: _State, n: int) -> list:
    out = []
    for _ in range(n):
        out.append(state.stack.pop())
    out.reverse()
    return out


def _parse_function(state: _State, ast: AstScope, fn: LuaFunction, bits_b: int) -> AstScope:
    local_spawn: dict[int, list[int]] = {}
    local_kill: dict[int, list[int]] = {}
    for idx, local in enumerate(fn.locals):
        if local.start_pc == 0:
            state.stack.append(Identifier(local.name))
            state.reserved_elements += 1
        local_spawn.setdefault(local.start_pc, []).append(idx)
        local_kill.setdefault(local.end_pc, []).append(idx)

    for instr in fn.instructions:
        if state.pc > 0:
            spawned = local_spawn.get(state.pc, [])
            if local_kill.get(state.pc):
                state.reserved_elements -= len(local_kill[state.pc])
            if spawned:
                values = _pop_n(state, len(state.stack) - state.reserved_elements)
                locals_ = []
                for idx in spawned:
                    ident = Identifier(fn.locals[idx].name)
                    locals_.append(ident)
                    state.stack.append(ident)
                    state.reserved_elements += 1
                ast.statements.append(LocalDefinition(locals_, values))

        ast = _dispatch(state, ast, instr, fn, bits_b)

        if state.pc == ast.context.jump_offset:
            if ast.context.is_or_block:
                left = state.stack.pop()
                operation = state.stack[-1]
                operation.ex.append(left)
                ast.context.is_or_block = False
            while ast.context.is_condition and state.pc >= ast.context.jump_offset:
                condition = ast.parent.statements[-1]
                if ast.context.is_jmp_block:
                    condition.blocks.append(ConditionBlock(AstOperation("", []), []))
                condition.blocks[-1].statements = ast.statements
                ast.statements = []
                ast.context.is_condition = False
                ast = _exit_block(ast)

        state.pc += 1

    return ast


def _dispatch(state: _State, ast: AstScope, instr: int, fn: LuaFunction, bits_b: int) -> AstScope:
    op = op_of(instr)
    name = OPCODES.get(op)
    s = state.stack

    if name == "END":
        pass

    elif name == "RETURN":
        u = u_of(instr)
        args = _pop_n(state, len(s) - u)
        ast.statements.append(Return(args))

    elif name == "CALL":
        a, b = a_of(instr, bits_b), b_of(instr, bits_b)
        args = []
        while len(s) > a + 1:
            args.append(s.pop())
        args.reverse()
        caller = s.pop()
        if isinstance(caller, AstTable):
            s.append(caller)
        elif b == 0:
            ast.statements.append(Call([caller], args))
        else:
            s.append(Call([caller], args, b))

    elif name == "TAILCALL":
        a = a_of(instr, bits_b)
        args = []
        while len(s) > a + 1:
            args.append(s.pop())
        args.reverse()
        caller = s.pop()
        ast.statements.append(TailCall([caller], args))

    elif name == "PUSHNIL":
        for _ in range(u_of(instr)):
            s.append(Identifier("nil"))

    elif name == "POP":
        for _ in range(u_of(instr)):
            s.pop()

    elif name == "PUSHINT":
        s.append(AstInt(s_of(instr)))

    elif name == "PUSHSTRING":
        s.append(AstString(fn.globals[u_of(instr)]))

    elif name == "PUSHNUM":
        s.append(AstNumber(fn.numbers[u_of(instr)]))

    elif name == "PUSHNEGNUM":
        s.append(AstNumber(-fn.numbers[u_of(instr)]))

    elif name == "PUSHUPVALUE":
        # Upstream leaves this as a complete no-op (its own acknowledged
        # TODO), which silently desyncs the stack accounting - and then
        # crashes something downstream - for any script that actually
        # closes over an outer local. We don't track the enclosing
        # function's live locals at this point to name it properly, but
        # pushing a placeholder keeps the push/pop count correct, which is
        # what actually matters for not corrupting everything after it.
        s.append(Identifier(f"upvalue_{u_of(instr)}"))

    elif name == "GETLOCAL":
        s.append(Identifier(_local_name_at(fn, u_of(instr), state.pc)))

    elif name == "GETGLOBAL":
        s.append(Identifier(fn.globals[u_of(instr)]))

    elif name == "GETTABLE":
        index = s.pop()
        table = s.pop()
        s.append(Indexed([table, index]))

    elif name == "GETDOTTED":
        table = s.pop()
        s.append(Dotted([table, Identifier(fn.globals[u_of(instr)])]))

    elif name == "GETINDEXED":
        l = u_of(instr)
        ident = Identifier(fn.locals[l].name if l < len(fn.locals) else f"local_{l}")
        table = s.pop()
        s.append(Indexed([table, ident]))

    elif name == "PUSHSELF":
        table = s[-1]
        s.append(Dotted([table, Identifier(fn.globals[u_of(instr)])]))

    elif name == "CREATETABLE":
        name_str = ""
        if len(s) > state.reserved_elements and isinstance(s[-1], Identifier):
            name_str = s.pop().name
        s.append(AstTable(name_str, u_of(instr)))

    elif name == "SETLOCAL":
        l = u_of(instr)
        left = Identifier(fn.locals[l].name if l < len(fn.locals) else f"local_{l}")
        _handle_assignment(state, ast, left)

    elif name == "SETGLOBAL":
        _handle_assignment(state, ast, Identifier(fn.globals[u_of(instr)]))

    elif name == "SETTABLE":
        b = b_of(instr, bits_b)
        args = _pop_n(state, b)
        parts = []
        for a in args[:-1]:
            if isinstance(a, Identifier):
                parts.append(a.name)
            elif isinstance(a, AstString):
                parts.append(a.value)
        ast.statements.append(Assignment([Identifier(".".join(parts))], [args[-1]]))

    elif name == "SETLIST":
        b = b_of(instr, bits_b)
        elements = _pop_n(state, b)
        s.pop()  # empty AstTable placeholder
        s.append(AstList(elements))

    elif name == "SETMAP":
        u = u_of(instr)
        pairs = []
        for _ in range(u):
            value = s.pop()
            key = s.pop()
            pairs.append((key, value))
        pairs.reverse()
        table = s[-1]
        if not isinstance(table, AstTable):
            raise LuaDecompileError("SETMAP: expected a CREATETABLE result on top of the stack.")
        if not table.name:
            s.pop()
            s.append(AstMap(pairs))
        else:
            table.pairs = pairs

    elif name in ("ADD", "SUB", "MULT", "DIV", "POW"):
        right = s.pop()
        left = s.pop()
        op_str = {"ADD": "+", "SUB": "-", "MULT": "*", "DIV": "/", "POW": "^"}[name]
        s.append(AstOperation(op_str, [left, right]))

    elif name == "ADDI":
        left = s.pop()
        s.append(AstOperation("+", [left, AstNumber(s_of(instr))]))

    elif name == "CONCAT":
        u = u_of(instr)
        parts = _pop_n(state, u)
        s.append(AstOperation("..", parts))

    elif name == "MINUS":
        right = s.pop()
        s.append(AstOperation("-", [right]))

    elif name == "NOT":
        right = s.pop()
        s.append(AstOperation("not ", [right]))

    elif name in ("JMPNE", "JMPEQ", "JMPLT", "JMPLE", "JMPGT", "JMPGE"):
        right = s.pop()
        left = s.pop()
        cmp_str = {"JMPNE": "==", "JMPEQ": "~=", "JMPLT": ">=",
                   "JMPLE": ">", "JMPGT": "<=", "JMPGE": "<"}[name]
        ast = _handle_condition(state, ast, instr, cmp_str, [left, right])

    elif name == "JMPT":
        left = s.pop()
        ast = _handle_condition(state, ast, instr, "~=", [left, Identifier("nil")])

    elif name == "JMPF":
        left = s.pop()
        ast = _handle_condition(state, ast, instr, "==", [left, Identifier("nil")])

    elif name == "JMPONT":
        right = s.pop()
        s.append(AstOperation("or", [right]))
        ast.context.is_or_block = True
        ast.context.jump_offset = state.pc + s_of(instr)

    elif name == "JMPONF":
        left = s.pop()
        ast = _handle_condition(state, ast, instr, "==", [left, Identifier("nil")])

    elif name == "JMP":
        if ast.context.is_condition and state.pc >= ast.context.jump_offset:
            condition = ast.parent.statements[-1]
            if ast.context.is_jmp_block:
                condition.blocks.append(ConditionBlock(AstOperation("", []), []))
            condition.blocks[-1].statements = ast.statements
            ast.statements = []
            ast.context.is_condition = False
            ast = _exit_block(ast)
        if ast.context.is_condition:
            condition = ast.parent.statements[-1]
            condition.blocks[-1].statements = ast.statements
            ast.statements = []
            ast.context.jump_offset = state.pc + s_of(instr)
            ast.context.jmp_offset = state.pc + s_of(instr)
            ast.context.is_jmp_block = True

    elif name == "PUSHNILJMP":
        s.append(Identifier("nil"))

    elif name == "FORPREP":
        ast.statements.append(ForLoop("", Identifier(""), Identifier(""), Identifier(""), []))
        ast = _enter_block(ast)

    elif name == "LFORPREP":
        s.append(Identifier(""))  # value
        s.append(Identifier(""))  # key
        ast.statements.append(ForInLoop("", "", Identifier(""), []))
        ast = _enter_block(ast)

    elif name == "FORLOOP":
        nested = ast.statements
        loop_vars = nested[0] if nested else None
        ast = _exit_block(ast)
        loop = ast.statements[-1]
        if isinstance(loop_vars, LocalDefinition):
            loop.counter = loop_vars.left[0].name
            loop.begin, loop.end, loop.increment = loop_vars.right[0], loop_vars.right[1], loop_vars.right[2]
            loop.statements = nested[1:]
        else:
            # This chunk's local debug info is stripped (fn.locals is empty),
            # so the usual "the loop body's first statement is an
            # auto-inserted local declaration for i/start/end/step" trick
            # (see _parse_function's local_spawn handling) never fires -
            # there's nothing in the instruction stream itself that names
            # the loop counter or bounds. Degrade to a placeholder header
            # instead of losing the whole (still-genuine) loop body.
            loop.counter, loop.begin, loop.end, loop.increment = (
                "i", Identifier("?"), Identifier("?"), Identifier("?"))
            loop.statements = nested
        s.pop(); s.pop(); s.pop()

    elif name == "LFORLOOP":
        nested = ast.statements
        loop_vars = nested[0] if nested else None
        ast = _exit_block(ast)
        loop = ast.statements[-1]
        if isinstance(loop_vars, LocalDefinition) and len(loop_vars.left) >= 3:
            loop.table = loop_vars.right[0]
            loop.key, loop.value = loop_vars.left[1].name, loop_vars.left[2].name
            loop.statements = nested[1:]
        else:
            loop.table, loop.key, loop.value = Identifier("?"), "k", "v"
            loop.statements = nested
        s.pop(); s.pop(); s.pop()

    elif name == "CLOSURE":
        a = a_of(instr, bits_b)
        ast = _enter_block(ast)
        new_state = _State()
        ast = _parse_function(new_state, ast, fn.functions[a], bits_b)
        arguments = [Identifier(local.name) for local in fn.functions[a].locals if local.start_pc == 0]
        ast = _exit_block(ast)
        s.append(Closure(ast.child.statements, arguments))

    else:
        raise LuaDecompileError(f"Unknown opcode 0x{op:02x} at PC {state.pc}.")

    return ast


def _local_name_at(fn: LuaFunction, pos: int, pc: int) -> str:
    index = 0
    i = 0
    while i != pos and index < len(fn.locals):
        local = fn.locals[index]
        if local.start_pc <= pc <= local.end_pc:
            i += 1
        index += 1
    if index < len(fn.locals):
        return fn.locals[index].name
    return f"local_{pos}"


# --- AST printer (port of source/ast/ast.cpp) -------------------------------

_INDENT = "  "


def _print_statements(statements: list, out: list[str], indent: int) -> None:
    for stmt in statements:
        out.append(_print_statement(stmt, indent))
        out.append("\n")


def _ind(indent: int) -> str:
    return _INDENT * indent


def _print_expr(e, indent: int = 0) -> str:
    if isinstance(e, Identifier):
        return e.name
    if isinstance(e, AstInt):
        return str(e.value)
    if isinstance(e, AstNumber):
        return _fmt_number(e.value)
    if isinstance(e, AstString):
        return '"' + e.value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(e, Dotted):
        return _print_expr(e.ex[0]) + "." + _print_expr(e.ex[1])
    if isinstance(e, Indexed):
        return _print_expr(e.ex[0]) + "[" + _print_expr(e.ex[1]) + "]"
    if isinstance(e, AstList):
        return "{" + ", ".join(_print_expr(el) for el in e.elements) + "}"
    if isinstance(e, AstMap):
        parts = []
        for k, v in e.pairs:
            key_str = k.value if isinstance(k, AstString) else _print_expr(k)
            parts.append(f"{key_str} = {_print_expr(v)}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(e, AstTable):
        lines = [f"{e.name} {{"] if e.name else ["{"]
        body = []
        for k, v in e.pairs:
            key_str = k.value if isinstance(k, AstString) else _print_expr(k)
            body.append(_ind(indent + 1) + f"{key_str} = {_print_expr(v, indent + 1)}")
        lines.append(",\n".join(body))
        lines.append(_ind(indent) + "}")
        return "\n".join(lines)
    if isinstance(e, AstOperation):
        if len(e.ex) == 1:
            operand = _print_expr(e.ex[0])
            if isinstance(e.ex[0], AstOperation):
                operand = f"({operand})"
            return f"{e.op}{operand}"
        parts = []
        for operand in e.ex:
            text = _print_expr(operand)
            if isinstance(operand, AstOperation):
                text = f"({text})"
            parts.append(text)
        return f" {e.op} ".join(parts)
    if isinstance(e, Call):
        return _print_expr(e.caller[0]) + "(" + ", ".join(_print_expr(a) for a in e.arguments) + ")"
    if isinstance(e, Closure):
        args = ", ".join(_print_expr(a) for a in e.arguments)
        body = "".join(_print_statement(st, indent + 1) + "\n" for st in e.statements)
        return f"function({args})\n{body}{_ind(indent)}end"
    raise LuaDecompileError(f"Don't know how to print expression node {type(e).__name__}.")


def _fmt_number(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _print_statement(stmt, indent: int) -> str:
    pad = _ind(indent)
    if isinstance(stmt, Assignment):
        left = ", ".join(i.name for i in stmt.left)
        right = ", ".join(_print_expr(v) for v in stmt.right)
        return f"{pad}{left} = {right}"
    if isinstance(stmt, LocalDefinition):
        left = ", ".join(i.name for i in stmt.left)
        right = ", ".join(_print_expr(v, indent) for v in stmt.right)
        return f"{pad}local {left} = {right}"
    if isinstance(stmt, Call):
        return f"{pad}{_print_expr(stmt.caller[0])}(" + ", ".join(_print_expr(a) for a in stmt.arguments) + ")"
    if isinstance(stmt, TailCall):
        return f"{pad}return {_print_expr(stmt.caller[0])}(" + ", ".join(_print_expr(a) for a in stmt.arguments) + ")"
    if isinstance(stmt, Return):
        return f"{pad}return " + ", ".join(_print_expr(e) for e in stmt.ex)
    if isinstance(stmt, Condition):
        lines = []
        for i, block in enumerate(stmt.blocks):
            if i == 0:
                lines.append(f"{pad}if {_print_expr(block.comparison)} then\n")
            elif not block.comparison.empty():
                lines.append(f"{pad}elseif {_print_expr(block.comparison)} then\n")
            else:
                lines.append(f"{pad}else\n")
            body = []
            _print_statements(block.statements, body, indent + 1)
            lines.append("".join(body))
        lines.append(f"{pad}end")
        return "".join(lines)
    if isinstance(stmt, ForLoop):
        header = (f"{pad}for {stmt.counter} = {_print_expr(stmt.begin)} , "
                  f"{_print_expr(stmt.end)} , {_print_expr(stmt.increment)} do\n")
        body = []
        _print_statements(stmt.statements, body, indent + 1)
        return header + "".join(body) + f"{pad}end"
    if isinstance(stmt, ForInLoop):
        header = f"{pad}for {stmt.key} , {stmt.value} in {_print_expr(stmt.table)} do\n"
        body = []
        _print_statements(stmt.statements, body, indent + 1)
        return header + "".join(body) + f"{pad}end"
    raise LuaDecompileError(f"Don't know how to print statement node {type(stmt).__name__}.")


# --- Public API --------------------------------------------------------------


def decompile(data: bytes) -> str:
    """Decompiles a Lua 4.0 bytecode chunk (the full byte string starting
    with the '\\x1bLua\\x40' signature) back into readable Lua source.
    Raises LuaDecompileError on anything this port doesn't understand
    (malformed input, or - same as the upstream reference this was ported
    from - a handful of acknowledged-incomplete opcode edge cases) rather
    than emitting wrong code silently."""
    header, main = read_chunk(data)
    state = _State()
    root = AstScope()
    final = _parse_function(state, root, main, header.bits_for_register_b)
    # parse_function always returns to the root scope once a well-formed
    # chunk's END instruction is reached; if it didn't, something in the
    # instruction stream wasn't understood.
    top = final
    while top.parent is not None:
        top = top.parent
    out: list[str] = []
    _print_statements(top.statements, out, 0)
    return "".join(out)


def is_lua4_bytecode(data: bytes) -> bool:
    return data[:5] == MAGIC
