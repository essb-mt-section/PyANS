import queue
import requests
from datetime import datetime
from json.decoder import JSONDecodeError
from multiprocessing import Event, Process, Queue
from time import sleep
from typing import Dict, List, Optional, Tuple, Union

from ._misc import flatten

DEFAULT_TIMEOUT = 5

class Cache(object):

    def __init__(self) -> None:
        self._cache = {}

    def get(self, key:str) -> Optional[Dict]:
        try:
            return self._cache[key]
        except KeyError:
            return None

    def add(self, key:str, value: Union[Dict, List[Dict]]) -> None:
        self._cache[key] = value

    def clear(self):
        self._cache = {}


def request_json(url, headers:Optional[Dict]=None,
                      ignore_http_error=False,
                      timeout:int=DEFAULT_TIMEOUT) -> Union[Dict, None, List[Dict]]:
    """online request of a dict (via json response), might raise JSONDecodeError
    return None, if ConnectionError or timeout
    """
    # print(url) #DEBUG
    try:
        req = requests.get(url.strip(), headers=headers, timeout=timeout)
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout):
        return None
    try:
        rtn = req.json()
    except JSONDecodeError:
        if not ignore_http_error:
            req.raise_for_status()
        rtn = None

    return rtn

class RequestProcess(Process):

    NOTHING_RECEIVED = {"ERROR": "<NOTHING RECEIVED>"}

    def __init__(self, url,  headers:Optional[Dict]=None,
                    request_timeout:int=DEFAULT_TIMEOUT,
                    ignore_http_error=False,
                    autostart=True):
        super().__init__()

        self.url = url
        self.request_timeout = request_timeout
        self.ignore_http_error = ignore_http_error
        self.header = headers
        self._queue = Queue()
        self._response = None
        self._has_response = Event()
        self.daemon = True
        if autostart:
            self.start()

    def has_response(self):
        return self._has_response.is_set()

    def get(self) -> Union[None , List[Dict], Dict]:
        """ returns None if still working,
        otherwise NOTHING_RECEIVED or the response"""

        if self._response is None and self.has_response():
            # process finished but not yet retrieved from queue
            try:
                self._response = self._queue.get()
            except queue.Empty: # should never happen
                pass
            self.terminate()

        return self._response

    def run(self):
        rtn = request_json(self.url, headers=self.header,
                           ignore_http_error=self.ignore_http_error,
                               timeout=self.request_timeout)
        if rtn is None:
            rtn = RequestProcess.NOTHING_RECEIVED
        self._queue.put(rtn)
        self._has_response.set()


class MultiplePagesRequestProcess(RequestProcess):
    # requestion multiple pages

    def __init__(self, url,
                 headers: Optional[Dict] = None,
                 request_timeout: int = DEFAULT_TIMEOUT,
                 autostart=True):
        """ if url must contain page counter tag {{cnt:x}}, where x is the start counter
        """
        super().__init__(url, headers, request_timeout,
                         autostart=False)
        self.start_cnt, self.items, self.url = _find_cnttag_items(url)
        if self.start_cnt is None:
            raise ValueError("Not counter tag in url {self.url}")

        if autostart:
            self.start()

    def run(self):
        rtn_lists = []
        cnt = self.start_cnt
        while True:
            url = self.url.format(cnt)
            # print(url) 3 debug
            new_list = request_json(url, headers=self.header,
                               timeout=self.request_timeout)
            cnt = cnt + 1 # type: ignore
            if isinstance(new_list, list) and len(new_list):
                if new_list in rtn_lists:
                    # new has already been received -> reached end
                    break

                rtn_lists.append(new_list)
                if len(new_list) < self.items: # type: ignore
                    # less than requested
                    break
            else:
                # no list received -> end
                break

        if len(rtn_lists) is None:
            self._queue.put(RequestProcess.NOTHING_RECEIVED)
        else:
            self._queue.put(flatten(rtn_lists))

        self._has_response.set()


class ProcessListFullError(Exception):
    pass


class RequestProcessManager(object):

    def __init__(self, cache:Optional[Cache], max_processes=4):

        self.process_list = []
        self.max_processes = max_processes
        self._cache = cache

    def n_working_threads(self) -> int:
        return sum([not(p[1].has_response()) for p in self.process_list])

    def n_threads(self) -> int:
        return len(self.process_list)

    def add_no_wait(self, who, thread:RequestProcess) -> None:
        """raises ProcessListFullError if current list contains
        too many working thread"""

        if self.n_working_threads() >= self.max_processes:
            raise ProcessListFullError
        else:
            self.process_list.append((who, thread))

    def add(self, who, thread:RequestProcess) -> None:
        """adds and wait is list is full"""
        while True:
            try:
                return self.add_no_wait(who=who, thread=thread)
            except ProcessListFullError:
                sleep(0.001)

    def get_finished(self) -> List[Tuple]:
        """returns list of tuple with the results of all threads
            (who, response) or empty list if no finished thread is in list

            writes also cache, if defined
        """

        still_working = []
        responses = []

        while len(self.process_list)>0:
            who, thr = self.process_list.pop(0)
            if thr.has_response():
                responses.append((who, thr.get()))
                if self._cache is not None:
                    self._cache.add(thr.url, thr.get())
            else:
                still_working.append((who, thr))

        self.process_list = still_working # put living threads back
        return responses


class TimeStampHistory(object):

    def __init__(self, max_size):
        self.max_size = max_size
        self.history = []

    def is_full(self):
        return self.max_size <= len(self.history)

    def timestamp(self):
        self.history.append(datetime.now())
        if len(self.history) > self.max_size:
            self.history.pop(0)

        #feedback("{}: {}".format(len(self.history), self.lag(0)))

    def lag(self, element=0):
        """current time difference to element x (default x=0,
        that is, time passed since oldest element in the history
        """
        return datetime.now() - self.history[element]


def _find_cnttag_items(txt:str)-> Tuple[Optional[int],Optional[int], str]:
    """return start counter, item and format string
    counter_tag: {{cnt:x}}, where x is the start counter
    """
    a = txt.find("{{cnt:")
    b = txt.find("}}")
    if a<0 or b<0:
        start_counter = None
    else:
        try:
            start_counter = int(txt[a+6:b])
        except ValueError:
            start_counter = None
    rtn_txt = txt[:a] + "{}" + txt[b+2:]

    if start_counter is not None:
        a = txt.find("items=")
        if a<0:
            item = None
        else:
            txtb = txt[a+6:]
            b = txtb.find("&")
            if b<0:
                b = len(txtb)
            try:
                item = int(txtb[:b])
            except ValueError:
                item = None

    else:
        item = None

    return start_counter, item, rtn_txt