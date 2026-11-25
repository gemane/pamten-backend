"""manage.py's commands: each is a `cmd_…` function with its arguments in an
`_args_…` function below it, listed once in `COMMANDS`.

Until 2026-11-25 all 57 commands' arguments sat in one 549-line `_build_parser`,
far from the functions they belong to. These tests keep the three parts of a
command together: a command without its arguments, or its arguments without a
line in `COMMANDS`, does not exist on the command line and nothing else says so."""
import argparse
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import manage  # noqa: E402


def _functions(prefix):
    return {name: fn for name, fn in vars(manage).items()
            if name.startswith(prefix) and inspect.isfunction(fn) and fn.__module__ == manage.__name__}


def _added_by(add_arguments):
    """[(command, its func)] one `_args_…` function adds to a fresh parser."""
    subparsers = argparse.ArgumentParser().add_subparsers()
    add_arguments(subparsers)
    return [(name, parser.get_default("func")) for name, parser in subparsers.choices.items()]


def test_every_command_has_its_arguments_and_its_line():
    commands, arguments = _functions("cmd_"), _functions("_args_")
    assert len(commands) >= 57
    assert {n.removeprefix("cmd_") for n in commands} == {n.removeprefix("_args_") for n in arguments}
    assert set(manage.COMMANDS) == set(arguments.values())
    assert len(manage.COMMANDS) == len(set(manage.COMMANDS))                 # nobody listed twice


def test_each_arguments_function_adds_one_command_that_runs_its_own_function():
    seen = set()
    for add_arguments in manage.COMMANDS:
        added = _added_by(add_arguments)
        assert len(added) == 1, add_arguments.__name__
        [(command, func)] = added
        assert func is getattr(manage, "cmd_" + add_arguments.__name__.removeprefix("_args_")), command
        assert command not in seen, command                                  # two functions, one command name
        seen.add(command)
        assert f"`manage.py {command}`" in add_arguments.__doc__


def test_the_arguments_sit_below_their_command():
    line = {name: inspect.getsourcelines(fn)[1] for name, fn in {**_functions("cmd_"), **_functions("_args_")}.items()}
    ordered = sorted(line, key=line.get)
    for name in _functions("cmd_"):
        assert ordered[ordered.index(name) + 1] == "_args_" + name.removeprefix("cmd_"), name


def test_the_parser_lists_the_commands_in_the_order_of_the_list():
    [subparsers] = [a for a in manage._build_parser()._actions if isinstance(a, argparse._SubParsersAction)]
    assert list(subparsers.choices) == [_added_by(add)[0][0] for add in manage.COMMANDS]
