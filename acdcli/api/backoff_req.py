import time
from time import sleep
import random
import logging
from threading import Lock, local

from .common import *

logger = logging.getLogger(__name__)

CONN_TIMEOUT = 30
"""timeout for establishing a connection"""
IDLE_TIMEOUT = 60
"""read timeout"""
REQUESTS_TIMEOUT = (CONN_TIMEOUT, IDLE_TIMEOUT) if requests.__version__ >= '2.4.0' else IDLE_TIMEOUT
"""http://docs.python-requests.org/en/latest/user/advanced/#timeouts"""


class BackOffRequest(object):
    """Wrapper for requests that implements timed back-off algorithm
    https://developer.amazon.com/public/apis/experience/cloud-drive/content/best-practices
    Caution: this catches all connection errors and may stall for a long time.
    It is necessary to init this module before use."""

    def __init__(self, auth_callback: 'requests.auth.AuthBase'):
        """:arg auth_callback: callable object that attaches auth info to a request"""

        self.auth_callback = auth_callback

        self.__session = requests.session()
        self.__thr_local = local()
        self.__lock = Lock()
        self.__retries = 0
        self.__next_req = time.time()

        random.seed()

    def _succeeded(self):
        with self.__lock:
            self.__retries = 0
        self.__calc_next()

    def _failed(self):
        with self.__lock:
            self.__retries += 1
        self.__calc_next()

    def __calc_next(self):
        """Calculates minimal acceptable time for next request.
        Back-off time is in a range of seconds, depending on number of failed previous tries (r):
        [0,2^r], maximum interval [0,256]"""
        with self.__lock:
            duration = random.random() * 2 ** min(self.__retries, 8)
            self.__next_req = time.time() + duration

    def _wait(self):
        with self.__lock:
            duration = self.__next_req - time.time()
        if duration > 5:
            logger.warning('Waiting %fs because of error(s).' % duration)
        logger.debug('Retry %i, waiting %fs' % (self.__retries, duration))
        if duration > 0:
            sleep(duration)

    @catch_conn_exception
    def _request(self, type_: str, url: str, acc_codes: 'List[int]', **kwargs) -> requests.Response:
        """Performs a HTTP request

        :param type_: the type of HTTP request to perform
        :param acc_codes: list of HTTP status codes that indicate a successful request
        :param kwargs: may include additional header: dict and timeout: int"""

        self._wait()

        headers = {}
        if 'headers' in kwargs:
            headers = dict(**(kwargs['headers']))
            del kwargs['headers']

        last_url = getattr(self.__thr_local, 'last_req_url', None)
        if url == last_url:
            logger.debug('%s "%s"' % (type_, url))
        else:
            logger.info('%s "%s"' % (type_, url))
        if 'data' in kwargs.keys():
            logger.debug(kwargs['data'])

        self.__thr_local.last_req_url = url

        if 'timeout' in kwargs:
            timeout = kwargs['timeout']
            del kwargs['timeout']
        else:
            timeout = REQUESTS_TIMEOUT

        exc = False
        try:
            r = self.__session.request(type_, url, auth=self.auth_callback,
                                 headers=headers, timeout=timeout, **kwargs)
        except:
            exc = True
            self._failed()
            raise
        finally:
            if (exc or r.status_code not in acc_codes) and 'x-amzn-RequestId' in r.headers:
                logger.info('Failed x-amzn-RequestId: %s' % r.headers['x-amzn-RequestId'])
            else:
                if 'x-amzn-RequestId' in r.headers:
                    logger.debug('x-amzn-RequestId: %s' % r.headers['x-amzn-RequestId'])

        self._succeeded() if r.status_code in acc_codes else self._failed()
        return r

    # HTTP verbs

    def get(self, url, acc_codes=OK_CODES, **kwargs) -> requests.Response:
        return self._request('GET', url, acc_codes, **kwargs)

    def post(self, url, acc_codes=OK_CODES, **kwargs) -> requests.Response:
        return self._request('POST', url, acc_codes, **kwargs)

    def patch(self, url, acc_codes=OK_CODES, **kwargs) -> requests.Response:
        return self._request('PATCH', url, acc_codes, **kwargs)

    def put(self, url, acc_codes=OK_CODES, **kwargs) -> requests.Response:
        return self._request('PUT', url, acc_codes, **kwargs)

    def delete(self, url, acc_codes=OK_CODES, **kwargs) -> requests.Response:
        return self._request('DELETE', url, acc_codes, **kwargs)

    def paginated_get(self, url: str, params: dict = None) -> 'List[dict]':
        """Gets node list in segments of 200."""
        if params is None:
            params = {}
        node_list = []

        while True:
            r = self.get(url, params=params)
            if r.status_code not in OK_CODES:
                logger.error("Error getting node list.")
                raise RequestError(r.status_code, r.text)
            ret = r.json()
            node_list.extend(ret['data'])
            if 'nextToken' in ret.keys():
                params['startToken'] = ret['nextToken']
            else:
                if ret['count'] != len(node_list):
                    logger.warning(
                        'Expected %i items in page, received %i.' % (ret['count'], len(node_list)))
                break

        return node_list
