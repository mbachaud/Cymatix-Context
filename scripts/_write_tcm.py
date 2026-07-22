import pathlib
target = pathlib.Path(__file__).parent.parent / "cymatix_context" / "tcm.py"
print(f"Writing to {target}")
# The actual content will be appended by subsequent calls
