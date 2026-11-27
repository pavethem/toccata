import faulthandler
import sys

faulthandler.enable()  # a GTK abort then prints the Python stack

if "--check" in sys.argv[1:]:
    from .check import run
    sys.exit(run())

from .app import main  # noqa: E402  (imports GTK, so only after the check)

sys.exit(main())
