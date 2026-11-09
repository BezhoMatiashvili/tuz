"""Public Shell LubeMatch car chooser for the en-GB vehicle search."""

from automotive.oats import OatsCatalog

SHELL_OATS = 'https://d126xd59kaz5rr.cloudfront.net/api/proxy/oats/shell-lmc-gb'


class ShellOilClient(OatsCatalog):
    def __init__(self) -> None:
        super().__init__('Shell', SHELL_OATS, 'en_GB')
