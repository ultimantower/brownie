#!/usr/bin/python3

import logging
from collections import deque
from hashlib import sha1
from typing import Any, Dict, List, Optional, Set, Tuple

import solcast
import solcx
from requests.exceptions import ConnectionError
from semantic_version import Version
from solcast.nodes import NodeBase

from brownie._config import EVM_EQUIVALENTS
from brownie.exceptions import CompilerError, IncompatibleSolcVersion
from brownie.project.compiler.utils import expand_source_map

from . import sources

solcx_logger = logging.getLogger("solcx")
solcx_logger.setLevel(10)
sh = logging.StreamHandler()
sh.setLevel(10)
sh.setFormatter(logging.Formatter("%(message)s"))
solcx_logger.addHandler(sh)

AVAILABLE_SOLC_VERSIONS = None


def get_version() -> Version:
    return solcx.get_solc_version().truncate()


def compile_from_input_json(
    input_json: Dict, silent: bool = True, allow_paths: Optional[str] = None
) -> Dict:

    """
    Compiles contracts from a standard input json.

    Args:
        input_json: solc input json
        silent: verbose reporting
        allow_paths: compiler allowed filesystem import path

    Returns: standard compiler output json
    """

    optimizer = input_json["settings"]["optimizer"]
    input_json["settings"].setdefault("evmVersion", None)
    if input_json["settings"]["evmVersion"] in EVM_EQUIVALENTS:
        input_json["settings"]["evmVersion"] = EVM_EQUIVALENTS[input_json["settings"]["evmVersion"]]

    if not silent:
        print("Compiling contracts...")
        print(f"  Solc version: {str(solcx.get_solc_version())}")

        print(
            "  Optimizer: "
            + (f"Enabled  Runs: {optimizer['runs']}" if optimizer["enabled"] else "Disabled")
        )
        if input_json["settings"]["evmVersion"]:
            print(f"  EVM Version: {input_json['settings']['evmVersion'].capitalize()}")

    try:
        return solcx.compile_standard(
            input_json,
            optimize=optimizer["enabled"],
            optimize_runs=optimizer["runs"],
            evm_version=input_json["settings"]["evmVersion"],
            allow_paths=allow_paths,
        )
    except solcx.exceptions.SolcError as e:
        raise CompilerError(e)


def set_solc_version(version: str) -> str:
    """Sets the solc version. If not available it will be installed."""
    if Version(version.lstrip("v")) < Version("0.4.22"):
        raise IncompatibleSolcVersion("Brownie only supports Solidity versions >=0.4.22")
    try:
        solcx.set_solc_version(version, silent=True)
    except solcx.exceptions.SolcNotInstalled:
        install_solc(version)
        solcx.set_solc_version(version, silent=True)
    return str(solcx.get_solc_version())


def install_solc(*versions: str) -> None:
    """Installs solc versions."""
    for version in versions:
        solcx.install_solc(str(version), show_progress=True)


def get_abi(contract_source: str) -> Dict:
    """Given a contract source, returns a dict of {name: abi}"""
    version = find_best_solc_version({"<stdin>": contract_source})
    set_solc_version(version)
    compiled = solcx.compile_source(contract_source, allow_empty=True, output_values=["abi"])
    return {k.rsplit(":")[-1]: v["abi"] for k, v in compiled.items()}


def find_solc_versions(
    contract_sources: Dict[str, str],
    install_needed: bool = False,
    install_latest: bool = False,
    silent: bool = True,
) -> Dict:

    """
    Analyzes contract pragmas and determines which solc version(s) to use.

    Args:
        contract_sources: a dictionary in the form of {'path': "source code"}
        install_needed: if True, will install when no installed version matches
                        the contract pragma
        install_latest: if True, will install when a newer version is available
                        than the installed one
        silent: set to False to enable verbose reporting

    Returns: dictionary of {'version': ['path', 'path', ..]}
    """

    available_versions, installed_versions = _get_solc_version_list()

    pragma_specs: Dict = {}
    to_install = set()
    new_versions = set()

    for path, source in contract_sources.items():
        pragma_specs[path] = sources.get_pragma_spec(source, path)
        version = pragma_specs[path].select(installed_versions)

        if not version and not (install_needed or install_latest):
            raise IncompatibleSolcVersion(
                f"No installed solc version matching '{pragma_specs[path]}' in '{path}'"
            )

        # if no installed version of solc matches the pragma, find the latest available version
        latest = pragma_specs[path].select(available_versions)

        if not version and not latest:
            raise IncompatibleSolcVersion(
                f"No installable solc version matching '{pragma_specs[path]}' in '{path}'"
            )

        if not version or (install_latest and latest > version):
            to_install.add(latest)
        elif latest and latest > version:
            new_versions.add(str(version))

    # install new versions if needed
    if to_install:
        install_solc(*to_install)
        installed_versions = [Version(i[1:]) for i in solcx.get_installed_solc_versions()]
    elif new_versions and not silent:
        print(
            f"New compatible solc version{'s' if len(new_versions) > 1 else ''}"
            f" available: {', '.join(new_versions)}"
        )

    # organize source paths by latest available solc version
    compiler_versions: Dict = {}
    for path, spec in pragma_specs.items():
        version = spec.select(installed_versions)
        compiler_versions.setdefault(str(version), []).append(path)

    return compiler_versions


def find_best_solc_version(
    contract_sources: Dict[str, str],
    install_needed: bool = False,
    install_latest: bool = False,
    silent: bool = True,
) -> str:

    """
    Analyzes contract pragmas and finds the best version compatible with all sources.

    Args:
        contract_sources: a dictionary in the form of {'path': "source code"}
        install_needed: if True, will install when no installed version matches
                        the contract pragma
        install_latest: if True, will install when a newer version is available
                        than the installed one
        silent: set to False to enable verbose reporting

    Returns: version string
    """

    available_versions, installed_versions = _get_solc_version_list()

    for path, source in contract_sources.items():

        pragma_spec = sources.get_pragma_spec(source, path)
        installed_versions = [i for i in installed_versions if i in pragma_spec]
        available_versions = [i for i in available_versions if i in pragma_spec]

    if not available_versions:
        raise IncompatibleSolcVersion("No installable solc version compatible across all sources")

    if not installed_versions and not (install_needed or install_latest):
        raise IncompatibleSolcVersion("No installed solc version compatible across all sources")

    if max(available_versions) > max(installed_versions, default=Version("0.0.0")):
        if install_latest or (install_needed and not installed_versions):
            install_solc(max(available_versions))
            return str(max(available_versions))
        if not silent:
            print(f"New compatible solc version available: {max(available_versions)}")

    return str(max(installed_versions))


def _get_solc_version_list() -> Tuple[List, List]:
    global AVAILABLE_SOLC_VERSIONS
    installed_versions = [Version(i[1:]) for i in solcx.get_installed_solc_versions()]
    if AVAILABLE_SOLC_VERSIONS is None:
        try:
            AVAILABLE_SOLC_VERSIONS = [Version(i[1:]) for i in solcx.get_available_solc_versions()]
        except ConnectionError:
            if not installed_versions:
                raise ConnectionError("Solc not installed and cannot connect to GitHub")
            AVAILABLE_SOLC_VERSIONS = installed_versions
    return AVAILABLE_SOLC_VERSIONS, installed_versions


def _get_unique_build_json(
    output_evm: Dict, contract_node: Any, stmt_nodes: Dict, branch_nodes: Dict, has_fallback: bool
) -> Dict:
    paths = sorted(
        set(
            [contract_node.parent().absolutePath]
            + [i.parent().absolutePath for i in contract_node.dependencies]
        )
    )

    bytecode = _format_link_references(output_evm)
    pc_map, statement_map, branch_map = _generate_coverage_data(
        output_evm["deployedBytecode"]["sourceMap"],
        output_evm["deployedBytecode"]["opcodes"],
        contract_node,
        stmt_nodes,
        branch_nodes,
        has_fallback,
    )
    return {
        "allSourcePaths": paths,
        "bytecode": bytecode,
        "bytecodeSha1": sha1(bytecode[:-68].encode()).hexdigest(),
        "coverageMap": {"statements": statement_map, "branches": branch_map},
        "dependencies": [i.name for i in contract_node.dependencies],
        "offset": contract_node.offset,
        "pcMap": pc_map,
        "type": contract_node.contractKind,
    }


def _format_link_references(evm: Dict) -> Dict:
    # Standardizes formatting for unlinked libraries within bytecode
    bytecode = evm["bytecode"]["object"]
    references = [
        (k, x) for v in evm["bytecode"].get("linkReferences", {}).values() for k, x in v.items()
    ]
    for n, loc in [(i[0], x["start"] * 2) for i in references for x in i[1]]:
        bytecode = f"{bytecode[:loc]}__{n[:36]:_<36}__{bytecode[loc+40:]}"
    return bytecode


def _generate_coverage_data(
    source_map_str: str,
    opcodes_str: str,
    contract_node: Any,
    stmt_nodes: Dict,
    branch_nodes: Dict,
    has_fallback: bool,
) -> Tuple:
    # Generates data used by Brownie for debugging and coverage evaluation
    if not opcodes_str:
        return {}, {}, {}

    source_map = deque(expand_source_map(source_map_str))
    opcodes = deque(opcodes_str.split(" "))

    contract_nodes = [contract_node] + contract_node.dependencies
    source_nodes = dict((i.contract_id, i.parent()) for i in contract_nodes)
    paths = set(v.absolutePath for v in source_nodes.values())

    stmt_nodes = dict((i, stmt_nodes[i].copy()) for i in paths)
    statement_map: Dict = dict((i, {}) for i in paths)

    # possible branch offsets
    branch_original = dict((i, branch_nodes[i].copy()) for i in paths)
    branch_nodes = dict((i, set(i.offset for i in branch_nodes[i])) for i in paths)
    # currently active branches, awaiting a jumpi
    branch_active: Dict = dict((i, {}) for i in paths)
    # branches that have been set
    branch_set: Dict = dict((i, {}) for i in paths)

    count, pc = 0, 0
    pc_list: List = []
    revert_map: Dict = {}
    fallback_hexstr: str = "unassigned"

    while source_map:
        # format of source is [start, stop, contract_id, jump code]
        source = source_map.popleft()
        pc_list.append({"op": opcodes.popleft(), "pc": pc})

        if (
            has_fallback is False
            and fallback_hexstr == "unassigned"
            and pc_list[-1]["op"] == "REVERT"
            and [i["op"] for i in pc_list[-4:-1]] == ["JUMPDEST", "PUSH1", "DUP1"]
        ):
            # flag the REVERT op at the end of the function selector,
            # later reverts may jump to it instead of having their own REVERT op
            fallback_hexstr = "0x" + hex(pc - 4).upper()[2:]
            pc_list[-1]["first_revert"] = True

        if source[3] != "-":
            pc_list[-1]["jump"] = source[3]

        pc += 1
        if opcodes[0][:2] == "0x":
            pc_list[-1]["value"] = opcodes.popleft()
            pc += int(pc_list[-1]["op"][4:])

        # set contract path (-1 means none)
        if source[2] == -1:
            if pc_list[-1]["op"] == "REVERT" and pc_list[-8]["op"] == "CALLVALUE":
                pc_list[-1].update(
                    {
                        "dev": "Cannot send ether to nonpayable function",
                        "fn": pc_list[-8].get("fn", "<unknown>"),
                        "offset": pc_list[-8]["offset"],
                        "path": pc_list[-8]["path"],
                    }
                )
            continue
        path = source_nodes[source[2]].absolutePath
        pc_list[-1]["path"] = path

        # set source offset (-1 means none)
        if source[0] == -1:
            continue
        offset = (source[0], source[0] + source[1])
        pc_list[-1]["offset"] = offset

        # add error messages for INVALID opcodes
        if pc_list[-1]["op"] == "INVALID":
            node = source_nodes[source[2]].children(include_children=False, offset_limits=offset)[0]
            if node.nodeType == "IndexAccess":
                pc_list[-1]["dev"] = "Index out of range"
            elif node.nodeType == "BinaryOperation":
                if node.operator == "/":
                    pc_list[-1]["dev"] = "Division by zero"
                elif node.operator == "%":
                    pc_list[-1]["dev"] = "Modulus by zero"

        # if op is jumpi, set active branch markers
        if branch_active[path] and pc_list[-1]["op"] == "JUMPI":
            for offset in branch_active[path]:
                # ( program counter index, JUMPI index)
                branch_set[path][offset] = (branch_active[path][offset], len(pc_list) - 1)
            branch_active[path].clear()

        # if op relates to previously set branch marker, clear it
        elif offset in branch_nodes[path]:
            if offset in branch_set[path]:
                del branch_set[path][offset]
            branch_active[path][offset] = len(pc_list) - 1

        try:
            # set fn name and statement coverage marker
            if "offset" in pc_list[-2] and offset == pc_list[-2]["offset"]:
                pc_list[-1]["fn"] = pc_list[-2]["fn"]
            else:
                pc_list[-1]["fn"] = _get_fn_full_name(source_nodes[source[2]], offset)
                stmt_offset = next(
                    i for i in stmt_nodes[path] if sources.is_inside_offset(offset, i)
                )
                stmt_nodes[path].discard(stmt_offset)
                statement_map[path].setdefault(pc_list[-1]["fn"], {})[count] = stmt_offset
                pc_list[-1]["statement"] = count
                count += 1
        except (KeyError, IndexError, StopIteration):
            pass
        if "value" not in pc_list[-1]:
            continue
        if pc_list[-1]["value"] == fallback_hexstr and opcodes[0] in {"JUMP", "JUMPI"}:
            # track all jumps to the initial revert
            revert_map.setdefault((pc_list[-1]["path"], pc_list[-1]["offset"]), []).append(
                len(pc_list)
            )

    # compare revert() statements against the map of revert jumps to find
    for (path, fn_offset), values in revert_map.items():
        fn_node = next(i for i in source_nodes.values() if i.absolutePath == path).children(
            depth=2,
            include_children=False,
            required_offset=fn_offset,
            filters={"nodeType": "FunctionDefinition"},
        )[0]
        revert_nodes = fn_node.children(
            filters={"nodeType": "FunctionCall", "expression.name": "revert"}
        )
        # if the node has arguments it will always be included in the source map
        for node in (i for i in revert_nodes if not i.arguments):
            offset = node.offset
            # if the node offset is not in the source map, apply it's offset to the JUMPI op
            if not next((x for x in pc_list if "offset" in x and x["offset"] == offset), False):
                pc_list[values[0]].update({"offset": offset, "jump_revert": True})
                del values[0]

    # set branch index markers and build final branch map
    branch_map: Dict = dict((i, {}) for i in paths)
    for path, offset, idx in [(k, x, y) for k, v in branch_set.items() for x, y in v.items()]:
        # for branch to be hit, need an op relating to the source and the next JUMPI
        # this is because of how the compiler optimizes nested BinaryOperations
        if "fn" not in pc_list[idx[0]]:
            continue
        fn = pc_list[idx[0]]["fn"]
        pc_list[idx[0]]["branch"] = count
        pc_list[idx[1]]["branch"] = count
        node = next(i for i in branch_original[path] if i.offset == offset)
        branch_map[path].setdefault(fn, {})[count] = offset + (node.jump,)
        count += 1

    pc_map = dict((i.pop("pc"), i) for i in pc_list)
    return pc_map, statement_map, branch_map


def _get_fn_full_name(source_node: NodeBase, offset: Tuple[int, int]) -> str:
    node = source_node.children(
        depth=2, required_offset=offset, filters={"nodeType": "FunctionDefinition"}
    )[0]
    name = getattr(node, "name", None)
    if not name:
        if getattr(node, "kind", "function") != "function":
            name = f"<{node.kind}>"
        elif getattr(node, "isConstructor", False):
            name = "<constructor>"
        else:
            name = "<fallback>"
    return f"{node.parent().name}.{name}"


def _get_nodes(output_json: Dict) -> Tuple[Dict, Dict, Dict]:
    source_nodes = solcast.from_standard_output(output_json)
    stmt_nodes = _get_statement_nodes(source_nodes)
    branch_nodes = _get_branch_nodes(source_nodes)
    return source_nodes, stmt_nodes, branch_nodes


def _get_statement_nodes(source_nodes: Dict) -> Dict:
    # Given a list of source nodes, returns a dict of lists of statement nodes
    statements = {}
    for node in source_nodes:
        statements[node.absolutePath] = set(
            i.offset
            for i in node.children(
                include_parents=False,
                filters={"baseNodeType": "Statement"},
                exclude_filter={"isConstructor": True},
            )
        )
    return statements


def _get_branch_nodes(source_nodes: List) -> Dict:
    # Given a list of source nodes, returns a dict of lists of nodes corresponding
    # to possible branches in the code
    branches: Dict = {}
    for node in source_nodes:
        branches[node.absolutePath] = set()
        for contract_node in node.children(depth=1, filters={"nodeType": "ContractDefinition"}):
            for child_node in [
                x
                for i in contract_node
                for x in i.children(
                    filters=(
                        {"nodeType": "FunctionCall", "expression.name": "require"},
                        {"nodeType": "IfStatement"},
                        {"nodeType": "Conditional"},
                    )
                )
            ]:
                branches[node.absolutePath] |= _get_recursive_branches(child_node)
    return branches


def _get_recursive_branches(base_node: Any) -> Set:
    # if node is IfStatement or Conditional, look only at the condition
    node = base_node if base_node.nodeType == "FunctionCall" else base_node.condition
    # for IfStatement, jumping indicates evaluating false
    jump_is_truthful = base_node.nodeType != "IfStatement"

    filters = (
        {"nodeType": "BinaryOperation", "typeDescriptions.typeString": "bool", "operator": "||"},
        {"nodeType": "BinaryOperation", "typeDescriptions.typeString": "bool", "operator": "&&"},
    )
    all_binaries = node.children(include_parents=True, include_self=True, filters=filters)

    # if no BinaryOperation nodes are found, this node is the branch
    if not all_binaries:
        # if node is FunctionCall, look at the first argument
        if base_node.nodeType == "FunctionCall":
            node = node.arguments[0]
        # some versions of solc do not map IfStatement unary opertions to bytecode
        elif node.nodeType == "UnaryOperation":
            node = node.subExpression
        node.jump = jump_is_truthful
        return set([node])

    # look at children of BinaryOperation nodes to find all possible branches
    binary_branches = set()
    for node in (x for i in all_binaries for x in (i.leftExpression, i.rightExpression)):
        if node.children(include_self=True, filters=filters):
            continue
        _jump = jump_is_truthful
        if not _is_rightmost_operation(node, base_node.depth):
            _jump = _check_left_operator(node, base_node.depth)
        if node.nodeType == "UnaryOperation":
            node = node.subExpression
        node.jump = _jump
        binary_branches.add(node)

    return binary_branches


def _is_rightmost_operation(node: NodeBase, depth: int) -> bool:
    # Check if the node is the final operation within the expression
    parents = node.parents(
        depth, {"nodeType": "BinaryOperation", "typeDescriptions.typeString": "bool"}
    )
    return not next(
        (i for i in parents if i.leftExpression == node or node.is_child_of(i.leftExpression)),
        False,
    )


def _check_left_operator(node: NodeBase, depth: int) -> bool:
    # Find the nearest parent boolean where this node sits on the left side of
    # the comparison, and return True if that node's operator is ||
    parents = node.parents(
        depth, {"nodeType": "BinaryOperation", "typeDescriptions.typeString": "bool"}
    )
    op = next(
        i for i in parents if i.leftExpression == node or node.is_child_of(i.leftExpression)
    ).operator
    return op == "||"
