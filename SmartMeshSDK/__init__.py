# SmartMeshSDK (ported to Python 3.10+ for the Voyager3 project)
# The original SDK docstrings contain backslash sequences (e.g. \problem) that
# trigger cosmetic SyntaxWarnings under Python 3. They do not affect behavior.
import warnings as _warnings
_warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"SmartMeshSDK.*")
