# ##############################################################################
#  Author: echel0n <echel0n@sickrage.ca>
#  URL: https://sickrage.ca/
#  Git: https://git.sickrage.ca/SiCKRAGE/sickrage.git
#  -
#  This file is part of SiCKRAGE.
#  -
#  SiCKRAGE is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#  -
#  SiCKRAGE is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  -
#  You should have received a copy of the GNU General Public License
#  along with SiCKRAGE.  If not, see <http://www.gnu.org/licenses/>.
# ##############################################################################


import os
import re
import time
from collections import OrderedDict
from threading import Lock

from dateutil import parser
from sqlalchemy import orm

import sickrage
from sickrage.core.databases.main import MainDB
from sickrage.core import scene_exceptions, common
from sickrage.core.exceptions import MultipleShowObjectsException
from sickrage.core.helpers import remove_extension, strip_accents
from sickrage.core.nameparser import regexes
from sickrage.core.scene_numbering import get_absolute_number_from_season_and_episode, get_indexer_absolute_numbering, get_indexer_numbering
from sickrage.core.tv.show.helpers import find_show_by_name, find_show
from sickrage.indexers import IndexerApi
from sickrage.indexers.exceptions import indexer_episodenotfound, indexer_error


class NameParser(object):
    ALL_REGEX = 0
    NORMAL_REGEX = 1
    ANIME_REGEX = 2

    def __init__(self, file_name=True, show_id=None, naming_pattern=False, validate_show=True):
        self.file_name = file_name
        self.show_obj = find_show(show_id)
        self.naming_pattern = naming_pattern
        self.validate_show = validate_show

        if self.show_obj and not self.show_obj.is_anime:
            self._compile_regexes(self.NORMAL_REGEX)
        elif self.show_obj and self.show_obj.is_anime:
            self._compile_regexes(self.ANIME_REGEX)
        else:
            self._compile_regexes(self.ALL_REGEX)

    def get_show(self, name):
        show_id = None
        show_names = [name]

        if not name:
            return show_id

        def cache_lookup(term):
            return sickrage.app.name_cache.get(term)

        def scene_exception_lookup(term):
            return scene_exceptions.get_scene_exception_by_name(term)[0]

        def showlist_lookup(term):
            try:
                return find_show_by_name(term).indexer_id
            except MultipleShowObjectsException:
                return None

        show_names.append(strip_accents(name))
        show_names.append(strip_accents(name).replace("'", " "))

        for show_name in set(show_names):
            lookup_list = [
                lambda: cache_lookup(show_name),
                lambda: scene_exception_lookup(show_name),
                lambda: showlist_lookup(show_name),
            ]

            # lookup show id
            for lookup in lookup_list:
                try:
                    show_id = int(lookup())
                    if show_id == 0:
                        continue

                    sickrage.app.name_cache.put(show_name, show_id)

                    if self.validate_show and not find_show(show_id):
                        continue
                except Exception:
                    pass

            if show_id is None:
                # ignore show name by caching it with a indexer_id of 0
                sickrage.app.name_cache.put(show_name, 0)

        return show_id or 0

    @staticmethod
    def clean_series_name(series_name):
        """Cleans up series name by removing any . and _
        characters, along with any trailing hyphens.

        Is basically equivalent to replacing all _ and . with a
        space, but handles decimal numbers in string, for example:
        """

        series_name = re.sub(r"(\D)\.(?!\s)(\D)", "\\1 \\2", series_name)
        series_name = re.sub(r"(\d)\.(\d{4})", "\\1 \\2", series_name)  # if it ends in a year then don't keep the dot
        series_name = re.sub(r"(\D)\.(?!\s)", "\\1 ", series_name)
        series_name = re.sub(r"\.(?!\s)(\D)", " \\1", series_name)
        series_name = series_name.replace("_", " ")
        series_name = re.sub(r"-$", "", series_name)
        series_name = re.sub(r"^\[.*\]", "", series_name)
        return series_name.strip()

    def _compile_regexes(self, regexMode):
        if regexMode == self.ANIME_REGEX:
            dbg_str = "ANIME"
            uncompiled_regex = [regexes.anime_regexes]
        elif regexMode == self.NORMAL_REGEX:
            dbg_str = "NORMAL"
            uncompiled_regex = [regexes.normal_regexes]
        else:
            dbg_str = "ALL"
            uncompiled_regex = [regexes.normal_regexes, regexes.anime_regexes]

        self.compiled_regexes = []
        for regexItem in uncompiled_regex:
            for cur_pattern_num, (cur_pattern_name, cur_pattern) in enumerate(regexItem):
                try:
                    cur_regex = re.compile(cur_pattern, re.VERBOSE | re.IGNORECASE)
                except re.error as errormsg:
                    sickrage.app.log.info(
                        "WARNING: Invalid episode_pattern using %s regexs, %s. %s" % (
                            dbg_str, errormsg, cur_pattern))
                else:
                    self.compiled_regexes.append((cur_pattern_num, cur_pattern_name, cur_regex))

    @MainDB.with_session
    def _parse_string(self, name, skip_scene_detection=False, session=None):
        if not name:
            return

        matches = []
        best_result = None

        for (cur_regex_num, cur_regex_name, cur_regex) in self.compiled_regexes:
            match = cur_regex.match(name)

            if not match:
                continue

            result = ParseResult(name)
            result.which_regex = {cur_regex_name}
            result.score = 0 - cur_regex_num

            named_groups = match.groupdict().keys()

            if 'series_name' in named_groups:
                result.series_name = match.group('series_name')
                if result.series_name:
                    result.series_name = self.clean_series_name(result.series_name)
                    result.score += 1

            if 'series_num' in named_groups and match.group('series_num'):
                result.score += 1

            if 'season_num' in named_groups:
                tmp_season = int(match.group('season_num'))
                if cur_regex_name == 'bare' and tmp_season in (19, 20):
                    continue
                if cur_regex_name == 'fov' and tmp_season > 500:
                    continue

                result.season_number = tmp_season
                result.score += 1

            if 'ep_num' in named_groups:
                ep_num = self._convert_number(match.group('ep_num'))
                if 'extra_ep_num' in named_groups and match.group('extra_ep_num'):
                    tmp_episodes = list(range(ep_num, self._convert_number(match.group('extra_ep_num')) + 1))
                    # if len(tmp_episodes) > 6:
                    #     continue
                else:
                    tmp_episodes = [ep_num]

                result.episode_numbers = tmp_episodes
                result.score += 3

            if 'ep_ab_num' in named_groups:
                ep_ab_num = self._convert_number(match.group('ep_ab_num'))
                result.score += 1

                if 'extra_ab_ep_num' in named_groups and match.group('extra_ab_ep_num'):
                    result.ab_episode_numbers = list(range(ep_ab_num,
                                                           self._convert_number(match.group('extra_ab_ep_num')) + 1))
                    result.score += 1
                else:
                    result.ab_episode_numbers = [ep_ab_num]

            if 'air_date' in named_groups:
                air_date = match.group('air_date')
                try:
                    result.air_date = parser.parse(air_date, fuzzy=True).date()
                    result.score += 1
                except Exception:
                    continue

            if 'extra_info' in named_groups:
                tmp_extra_info = match.group('extra_info')

                # Show.S04.Special or Show.S05.Part.2.Extras is almost certainly not every episode in the season
                if tmp_extra_info and cur_regex_name == 'season_only' and re.search(
                        r'([. _-]|^)(special|extra)s?\w*([. _-]|$)', tmp_extra_info, re.I):
                    continue
                result.extra_info = tmp_extra_info
                result.score += 1

            if 'release_group' in named_groups:
                result.release_group = match.group('release_group')
                result.score += 1

            if 'version' in named_groups:
                # assigns version to anime file if detected using anime regex. Non-anime regex receives -1
                version = match.group('version')
                if version:
                    result.version = version
                else:
                    result.version = 1
            else:
                result.version = -1

            matches.append(result)

        if len(matches):
            # pick best match with highest score based on placement
            best_result = max(sorted(matches, reverse=True, key=lambda x: x.which_regex), key=lambda x: x.score)

            show_obj = None
            best_result.indexer_id = self.show_obj.indexer_id if self.show_obj else 0

            if not self.naming_pattern:
                # try and create a show object for this result
                best_result.indexer_id = self.get_show(best_result.series_name)
                show_obj = find_show(best_result.indexer_id, session=session)

            # if this is a naming pattern test or result doesn't have a show object then return best result
            if not show_obj or self.naming_pattern:
                return best_result

            # get quality
            best_result.quality = common.Quality.name_quality(name, show_obj.is_anime)

            new_episode_numbers = []
            new_season_numbers = []
            new_absolute_numbers = []

            # if we have an air-by-date show then get the real season/episode numbers
            if best_result.is_air_by_date:
                try:
                    from sickrage.core.tv.episode import TVEpisode
                    dbData = session.query(TVEpisode).filter_by(showid=show_obj.indexer_id, indexer=show_obj.indexer, airdate=best_result.air_date).one()
                    season_number = int(dbData.season)
                    episode_numbers = [int(dbData.episode)]
                except (orm.exc.NoResultFound, orm.exc.MultipleResultsFound):
                    season_number = None
                    episode_numbers = []

                if not season_number or not episode_numbers:
                    try:
                        lINDEXER_API_PARMS = IndexerApi(show_obj.indexer).api_params.copy()

                        lINDEXER_API_PARMS['language'] = show_obj.lang or sickrage.app.config.indexer_default_language

                        t = IndexerApi(show_obj.indexer).indexer(**lINDEXER_API_PARMS)

                        epObj = t[show_obj.indexer_id].airedOn(best_result.air_date)[0]

                        season_number = int(epObj["airedseason"])
                        episode_numbers = [int(epObj["airedepisodenumber"])]
                    except indexer_episodenotfound:
                        if best_result.in_showlist:
                            sickrage.app.log.warning("Unable to find episode with date {air_date} for show {show}, "
                                                     "skipping".format(air_date=best_result.air_date, show=show_obj.name))
                        episode_numbers = []
                    except indexer_error as e:
                        sickrage.app.log.warning("Unable to contact " + IndexerApi(show_obj.indexer).name + ": {}".format(e))
                        episode_numbers = []

                for epNo in episode_numbers:
                    s = season_number
                    e = epNo

                    if show_obj.is_scene and not skip_scene_detection:
                        (s, e) = get_indexer_numbering(show_obj.indexer_id,
                                                       show_obj.indexer,
                                                       season_number,
                                                       epNo)
                    new_episode_numbers.append(e)
                    new_season_numbers.append(s)

            elif show_obj.is_anime and best_result.ab_episode_numbers:
                for epAbsNo in best_result.ab_episode_numbers:
                    a = epAbsNo

                    if show_obj.is_scene:
                        scene_season = scene_exceptions.get_scene_exception_by_name(best_result.series_name)[1]
                        a = get_indexer_absolute_numbering(show_obj.indexer_id,
                                                           show_obj.indexer, epAbsNo,
                                                           True, scene_season)

                    (s, e) = show_obj.get_all_episodes_from_absolute_number([a])

                    new_absolute_numbers.append(a)
                    new_episode_numbers.extend(e)
                    new_season_numbers.append(s)

            elif best_result.season_number and best_result.episode_numbers:
                for epNo in best_result.episode_numbers:
                    s = best_result.season_number
                    e = epNo

                    if show_obj.is_scene and not skip_scene_detection:
                        (s, e) = get_indexer_numbering(show_obj.indexer_id,
                                                       show_obj.indexer,
                                                       best_result.season_number,
                                                       epNo)
                    if show_obj.is_anime:
                        a = get_absolute_number_from_season_and_episode(show_obj.indexer_id, s, e)
                        if a:
                            new_absolute_numbers.append(a)

                    new_episode_numbers.append(e)
                    new_season_numbers.append(s)

            # need to do a quick sanity check heregex.  It's possible that we now have episodes
            # from more than one season (by tvdb numbering), and this is just too much
            # for sickrage, so we'd need to flag it.
            new_season_numbers = list(set(new_season_numbers))  # remove duplicates
            if len(new_season_numbers) > 1:
                raise InvalidNameException("Scene numbering results episodes from "
                                           "seasons %s, (i.e. more than one) and "
                                           "sickrage does not support this.  "
                                           "Sorry." % new_season_numbers)

            # I guess it's possible that we'd have duplicate episodes too, so lets
            # eliminate them
            new_episode_numbers = list(set(new_episode_numbers))
            new_episode_numbers.sort()

            # maybe even duplicate absolute numbers so why not do them as well
            new_absolute_numbers = list(set(new_absolute_numbers))
            new_absolute_numbers.sort()

            if len(new_absolute_numbers):
                best_result.ab_episode_numbers = new_absolute_numbers

            if len(new_season_numbers) and len(new_episode_numbers):
                best_result.episode_numbers = new_episode_numbers
                best_result.season_number = new_season_numbers[0]

            if show_obj.is_scene and not skip_scene_detection:
                sickrage.app.log.debug(
                    "Scene converted parsed result {} into {}".format(best_result.original_name, best_result))

        # CPU sleep
        time.sleep(0.02)

        return best_result

    def _combine_results(self, first, second, attr):
        # if the first doesn't exist then return the second or nothing
        if not first:
            if not second:
                return None
            else:
                return getattr(second, attr)

        # if the second doesn't exist then return the first
        if not second:
            return getattr(first, attr)

        a = getattr(first, attr)
        b = getattr(second, attr)

        # if a is good use it
        if a is not None or (isinstance(a, list) and a):
            return a
        # if not use b (if b isn't set it'll just be default)
        else:
            return b

    @staticmethod
    def _convert_number(org_number):
        """
         Convert org_number into an integer
         org_number: integer or representation of a number: string or unicode
         Try force converting to int first, on error try converting from Roman numerals
         returns integer or 0
         """

        try:
            # try forcing to int
            if org_number:
                number = int(org_number)
            else:
                number = 0

        except Exception:
            # on error try converting from Roman numerals
            roman_to_int_map = (
                ('M', 1000), ('CM', 900), ('D', 500), ('CD', 400), ('C', 100),
                ('XC', 90), ('L', 50), ('XL', 40), ('X', 10),
                ('IX', 9), ('V', 5), ('IV', 4), ('I', 1)
            )

            roman_numeral = org_number.upper()
            number = 0
            index = 0

            for numeral, integer in roman_to_int_map:
                while roman_numeral[index:index + len(numeral)] == numeral:
                    number += integer
                    index += len(numeral)

        return number

    def parse(self, name, cache_result=True, skip_scene_detection=False):
        if self.naming_pattern:
            cache_result = False

        cached = name_parser_cache.get(name)
        if cached:
            return cached

        # break it into parts if there are any (dirname, file name, extension)
        dir_name, file_name = os.path.split(name)

        base_file_name = file_name
        if self.file_name:
            base_file_name = remove_extension(file_name)

        # set up a result to use
        final_result = ParseResult(name)

        # try parsing the file name
        file_name_result = self._parse_string(base_file_name, skip_scene_detection)

        # use only the direct parent dir
        dir_name = os.path.basename(dir_name)

        # parse the dirname for extra info if needed
        dir_name_result = self._parse_string(dir_name, skip_scene_detection)

        # build the ParseResult object
        final_result.air_date = self._combine_results(file_name_result, dir_name_result, 'air_date')

        # anime absolute numbers
        final_result.ab_episode_numbers = self._combine_results(file_name_result, dir_name_result, 'ab_episode_numbers')

        # season and episode numbers
        final_result.season_number = self._combine_results(file_name_result, dir_name_result, 'season_number')
        final_result.episode_numbers = self._combine_results(file_name_result, dir_name_result, 'episode_numbers')

        # if the dirname has a release group/show name I believe it over the filename
        final_result.series_name = self._combine_results(dir_name_result, file_name_result, 'series_name')

        final_result.extra_info = self._combine_results(dir_name_result, file_name_result, 'extra_info')
        final_result.release_group = self._combine_results(dir_name_result, file_name_result, 'release_group')
        final_result.version = self._combine_results(dir_name_result, file_name_result, 'version')

        if final_result == file_name_result:
            final_result.which_regex = file_name_result.which_regex
        elif final_result == dir_name_result:
            final_result.which_regex = dir_name_result.which_regex
        else:
            if file_name_result:
                final_result.which_regex |= file_name_result.which_regex
            if dir_name_result:
                final_result.which_regex |= dir_name_result.which_regex

        final_result.indexer_id = self._combine_results(file_name_result, dir_name_result, 'indexer_id')
        final_result.quality = self._combine_results(file_name_result, dir_name_result, 'quality')

        if self.validate_show and not self.naming_pattern and not final_result.indexer_id:
            raise InvalidShowException("Unable to match {} to a show in your database. Parser result: {}".format(name, final_result))

        # if there's no useful info in it then raise an exception
        if final_result.season_number is None and not final_result.episode_numbers and final_result.air_date is None and not final_result.ab_episode_numbers and not final_result.series_name:
            raise InvalidNameException("Unable to parse {} to a valid episode. Parser result: {}".format(name, final_result))

        if cache_result and final_result.indexer_id:
            name_parser_cache.add(name, final_result)

        sickrage.app.log.debug("Parsed {} into {}".format(name, final_result))
        return final_result


class ParseResult(object):
    def __init__(self,
                 original_name,
                 series_name=None,
                 season_number=None,
                 episode_numbers=None,
                 extra_info=None,
                 release_group=None,
                 air_date=None,
                 ab_episode_numbers=None,
                 indexer_id=None,
                 score=None,
                 quality=None,
                 version=None
                 ):

        self.original_name = original_name
        self.series_name = series_name
        self.season_number = season_number
        self.episode_numbers = episode_numbers or []
        self.ab_episode_numbers = ab_episode_numbers or []
        self.quality = quality or common.Quality.UNKNOWN
        self.extra_info = extra_info
        self.release_group = release_group
        self.air_date = air_date
        self.indexer_id = indexer_id or 0
        self.score = score
        self.version = version
        self.which_regex = set()

    def __eq__(self, other):
        return other and all([
            self.__class__ == other.__class__,
            self.series_name == other.series_name,
            self.season_number == other.season_number,
            self.episode_numbers == other.episode_numbers,
            self.extra_info == other.extra_info,
            self.release_group == other.release_group,
            self.air_date == other.air_date,
            self.ab_episode_numbers == other.ab_episode_numbers,
            self.score == other.score,
            self.quality == other.quality,
            self.version == other.version
        ])

    def __str__(self):
        to_return = ""
        if self.series_name is not None:
            to_return += 'SHOW:[{}]'.format(self.series_name)
        if self.season_number is not None:
            to_return += ' SEASON:[{}]'.format(str(self.season_number).zfill(2))
        if self.episode_numbers and len(self.episode_numbers):
            to_return += ' EPISODE:[{}]'.format(','.join(str(x).zfill(2) for x in self.episode_numbers))
        if self.is_air_by_date:
            to_return += ' AIRDATE:[{}]'.format(self.air_date)
        if self.ab_episode_numbers:
            to_return += ' ABS:[{}]'.format(','.join(str(x).zfill(3) for x in self.ab_episode_numbers))
        if self.version and self.is_anime is True:
            to_return += ' ANIME VER:[{}]'.format(self.version)
        if self.release_group:
            to_return += ' GROUP:[{}]'.format(self.release_group)

        to_return += ' ABD:[{}]'.format(self.is_air_by_date)
        to_return += ' ANIME:[{}]'.format(self.is_anime)
        to_return += ' REGEX:[{}]'.format(' '.join(self.which_regex))

        return to_return

    @property
    def is_air_by_date(self):
        if self.air_date:
            return True
        return False

    @property
    def is_anime(self):
        if self.ab_episode_numbers:
            return True
        return False

    @property
    def in_showlist(self):
        if find_show(self.indexer_id):
            return True
        return False


class NameParserCache(object):
    def __init__(self):
        self.lock = Lock()
        self.data = OrderedDict()
        self.max_size = 200

    def get(self, key):
        with self.lock:
            value = self.data.get(key)
            if value:
                sickrage.app.log.debug("Using cached parse result for: {}".format(key))
            return value

    def add(self, key, value):
        with self.lock:
            self.data.update({key: value})
            while len(self.data) > self.max_size:
                self.data.pop(list(self.data.keys())[0], None)


name_parser_cache = NameParserCache()


class InvalidNameException(Exception):
    """The given release name is not valid"""


class InvalidShowException(Exception):
    """The given show name is not valid"""
