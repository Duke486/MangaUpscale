"""Compare filesystem paths and protect input directories."""

import os
from pathlib import Path


def same_location(first, second):
    normalize = lambda p: os.path.normcase(os.path.abspath(p))
    if normalize(first) == normalize(second):
        return True
    try:
        if Path(first).resolve() == Path(second).resolve():
            return True
    except OSError:
        pass
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def batch_output_root(source, output):
    source = Path(source).resolve()
    root = (Path(output) / source.name).resolve()
    if root == source or source in root.parents:
        raise ValueError("output_overlaps_input")
    return root


def saved_locations(config, source, output):
    if not same_location(source, config["source"]):
        raise ValueError("source_directory_changed")
    if not same_location(output, config["output"]):
        raise ValueError("output_directory_changed")
    # Members of an old manifest are relative to its saved, resolved source.
    # Continue using that same base after validating the user's alias.
    return Path(config["source"]), Path(config["output"])
