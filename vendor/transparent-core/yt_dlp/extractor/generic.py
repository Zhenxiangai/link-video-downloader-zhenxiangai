from .common import InfoExtractor
from ..utils import UnsupportedError


class GenericIE(InfoExtractor):
    IE_DESC = 'Unsupported URL fallback'
    IE_NAME = 'generic'
    _NETRC_MACHINE = False
    _VALID_URL = r'.*'

    def _real_extract(self, url):
        raise UnsupportedError(url)
