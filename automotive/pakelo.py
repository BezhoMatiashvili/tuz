"""Public Pakelo oil chooser. The page token is read per lookup and not stored."""

from automotive.oats import OatsCatalog


class PakeloClient(OatsCatalog):
    def __init__(self) -> None:
        super().__init__(
            'Pakelo', 'https://pakelo-wsc-it.phoenix.earlweb.net', 'en-GB',
            catalog_page='https://pakelo.com/en/find-your-oil',
        )
