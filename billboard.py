#!/usr/bin/env python

import datetime
import json
import re
import sys

from bs4 import BeautifulSoup
import requests

"""billboard.py: Unofficial Python API for accessing music charts from Billboard.com."""

__author__     = "Allen Guo"
__license__    = "MIT"
__maintainer__ = "Allen Guo"
__email__      = "guoguo12@gmail.com"


HEADERS = {
    'User-Agent': 'billboard.py (https://github.com/guoguo12/billboard-charts)'
}

# css selector constants
_CHART_NAME_SELECTOR = 'meta[name="twitter:title"]'
_DATE_ELEMENT_SELECTOR = 'button.chart-detail-header__date-selector-button'
_PREVIOUS_DATE_SELECTOR = 'span.fa-chevron-left'
_NEXT_DATE_SELECTOR = 'span.fa-chevron-right'
_TOP_TITLE_SELECTOR = 'div.chart-number-one__title'
_TOP_ARTIST_SELECTOR = 'div.chart-number-one__artist'
_TOP_LAST_POS_SELECTOR = 'div.chart-number-one__last-week'
_TOP_WEEKS_SELECTOR = 'div.chart-number-one__weeks-on-chart'
_NUMBER_ONE_NEW_SELECTOR = 'img.chart-number-one__stats-cell-new'
_ENTRY_LIST_SELECTOR = 'div.chart-list-item'
_ENTRY_TITLE_ATTR = 'data-title'
_ENTRY_ARTIST_ATTR = 'data-artist'
_ENTRY_RANK_ATTR = 'data-rank'

# constants for the getPositionRowValue helper function
_ROW_SELECTOR_FORMAT = 'div.chart-list-item__%s'
_PEAK_POS_FORMAT = 'weeks-at-one'
_LAST_POS_FORMAT = 'last-week'
_WEEKS_ON_CHART_FORMAT = 'weeks-on-chart'


class BillboardNotFoundException(Exception):
    pass


class BillboardParseException(Exception):
    pass


class ChartEntry:
    """Represents an entry (typically a single track) on a chart.

    Attributes:
        title: The title of the track.
        artist: The name of the track artist, as formatted on Billboard.com.
            If there are multiple artists and/or featured artists, they will
            be included in this string.
        peakPos: The track's peak position on the chart at any point in time,
            including future dates, as an int (or None if the chart does not
            include this information).
        lastPos: The track's position on the previous week's chart, as an int
            (or None if the chart does not include this information).
            This value is 0 if the track was not on the previous week's chart.
        weeks: The number of weeks the track has been or was on the chart,
            including future dates (up until the present time).
        rank: The track's position on the chart, as an int.
        isNew: Whether the track is new to the chart, as a boolean.
    """

    def __init__(self, title, artist, peakPos, lastPos, weeks, rank, isNew):
        self.title = title
        self.artist = artist
        self.peakPos = peakPos
        self.lastPos = lastPos
        self.weeks = weeks
        self.rank = rank
        self.isNew = isNew

    def __repr__(self):
        return '{}.{}(title={!r}, artist={!r})'.format(self.__class__.__module__,
                                                       self.__class__.__name__,
                                                       self.title,
                                                       self.artist)

    def __str__(self):
        """Returns a string of the form 'TITLE by ARTIST'.
        """
        if self.title:
            s = u"'%s' by %s" % (self.title, self.artist)
        else:
            s = u"%s" % self.artist

        if sys.version_info.major < 3:
            return s.encode(getattr(sys.stdout, 'encoding', '') or 'utf8')
        else:
            return s

    def json(self):
        """Returns the entry as a JSON string.
        This is useful for caching.
        """
        return json.dumps(self, default=lambda o: o.__dict__,
                          sort_keys=True, indent=4)


class ChartData:
    """Represents a particular Billboard chart for a particular date.

    Attributes:
        name: The chart name, as a string.
        title: The human-readable chart name, as a string.
        date: The date of the chart.
        previousDate: The date of the previous chart, as a string in YYYY-MM-DD
            format, or None if this information was not available.
        entries: A list of ChartEntry objects, ordered by position on the chart
            (highest first).
    """

    def __init__(self, name, date=None, fetch=True, timeout=25):
        """Constructs a new ChartData instance.

        Args:
            name: The chart name, e.g. 'hot-100' or 'pop-songs'.
                You can browse the Charts section of Billboard.com to find
                valid chart names; the URL of a chart will look like
                "http://www.billboard.com/charts/CHART-NAME".
            date: The chart date, as a string in YYYY-MM-DD format.
                By default, the latest chart is fetched.
                If the argument is not a date on which a chart was published,
                Billboard automatically rounds dates up to the nearest date on
                which a chart was published.
                If this argument is invalid, no exception will be raised;
                instead, the chart will contain no entries.
            fetch: A boolean indicating whether to fetch the chart data from
                Billboard.com immediately (at instantiation time).
                If False, the chart data can be populated at a later time
                using the fetchEntries() method.
            timeout: The number of seconds to wait for a server response.
                If None, no timeout is applied.
        """
        self.name = name

        if date is not None and not re.match('\d{4}-\d{2}-\d{2}', str(date)):
            raise ValueError('Date argument is not in YYYY-MM-DD format')
        self.date = date
        self.title = ''
        self.previousDate = None

        self._timeout = timeout

        self.entries = []
        if fetch:
            self.fetchEntries()

    def __repr__(self):
        return '{}.{}({!r}, date={!r})'.format(self.__class__.__module__,
                                               self.__class__.__name__,
                                               self.name, self.date)

    def __str__(self):
        """Returns the chart as a human-readable string (typically multi-line).
        """
        if not self.date:
            s = '%s chart (current)' % self.name
        else:
            s = '%s chart from %s' % (self.name, self.date)
        s += '\n' + '-' * len(s)
        for n, entry in enumerate(self.entries):
            s += '\n%s. %s' % (entry.rank, str(entry))
        return s

    def __getitem__(self, key):
        """Returns the (key + 1)-th chart entry; i.e., chart[0] refers to the
        top entry on the chart.
        """
        return self.entries[key]

    def __len__(self):
        """Returns the number of entries in the chart.
        A length of zero may indicated a failed/bad request.
        """
        return len(self.entries)

    def json(self):
        """Returns the entry as a JSON string.
        This is useful for caching.
        """
        return json.dumps(self, default=lambda o: o.__dict__,
                          sort_keys=True, indent=4)

    def fetchEntries(self):
        """GETs the corresponding chart data from Billboard.com, then parses
        the data using BeautifulSoup.
        """
        if not self.date:
            # Fetch latest chart
            url = 'http://www.billboard.com/charts/%s' % (self.name)
        else:
            url = 'http://www.billboard.com/charts/%s/%s' % (
                self.name, self.date)

        req = requests.get(url, headers=HEADERS, timeout=self._timeout)
        if req.status_code == 404:
            message = "Chart not found (perhaps the name is misspelled?)"
            raise BillboardNotFoundException(message)
        req.raise_for_status()

        soup = BeautifulSoup(req.text, 'html.parser')

        dateElement = soup.select_one(_DATE_ELEMENT_SELECTOR)
        if dateElement:
            dateText = dateElement.text.strip()
            self.date = datetime.datetime.strptime(dateText, '%B %d, %Y').strftime('%Y-%m-%d')

        chartTitleElement = soup.select_one(_CHART_NAME_SELECTOR)
        if chartTitleElement:
            self.title = chartTitleElement.get("content", "").split('|')[0].strip()

        prevWeek = soup.select_one(_PREVIOUS_DATE_SELECTOR)
        nextWeek = soup.select_one(_NEXT_DATE_SELECTOR)
        if prevWeek and prevWeek.parent.get('href'):
            self.previousDate = prevWeek.parent.get('href').split('/')[-1]
        if nextWeek and nextWeek.parent.get('href'):
            self.nextDate = nextWeek.parent.get('href').split('/')[-1]

        for entrySoup in soup.select(_ENTRY_LIST_SELECTOR):
            try:
                title = entrySoup[_ENTRY_TITLE_ATTR].strip()
            except:
                message = "Failed to parse title"
                raise BillboardParseException(message)

            try:
                artist = entrySoup[_ENTRY_ARTIST_ATTR].strip() or ''
            except:
                message = "Failed to parse artist"
                raise BillboardParseException(message)

            if artist == '':
                title, artist = artist, title

            try:
                rank = int(entrySoup[_ENTRY_RANK_ATTR].strip())
            except:
                message = "Failed to parse rank"
                raise BillboardParseException(message)

            def getPositionRowValue(rowName):
                try:
                    selector = _ROW_SELECTOR_FORMAT % rowName
                    selected = entrySoup.select_one(selector)
                    if selected is None or selected.string == '-':
                        return 0
                    else:
                        return int(selected.string.strip())
                except:
                    message = "Failed to parse row value: %s" % rowName
                    raise BillboardParseException(message)

            if self.date:
                peakPos = getPositionRowValue(_PEAK_POS_FORMAT)
                peakPos = rank if peakPos == 0 else peakPos
                lastPos = getPositionRowValue(_LAST_POS_FORMAT)
                weeks = getPositionRowValue(_WEEKS_ON_CHART_FORMAT)
                weeks = weeks if weeks != 0 else 1
                isNew = True if weeks == 1 else False
            else:
                peakPos = lastPos = weeks = None
                isNew = False

            entry = ChartEntry(title, artist, peakPos, lastPos, weeks, rank, isNew)
            self.entries.append(entry)
