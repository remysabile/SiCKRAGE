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


import datetime
import importlib
import inspect
import itertools
import os
import pkgutil
import random
import re
from base64 import b16encode, b32decode, b64decode
from collections import OrderedDict, defaultdict
from time import sleep
from urllib.parse import urljoin
from xml.sax import SAXParseException

from bencode3 import bdecode, bencode
from feedparser import FeedParserDict
from requests.utils import add_dict_to_cookiejar, dict_from_cookiejar

import sickrage
from sickrage.core.api.cache import TorrentCacheAPI
from sickrage.core.caches.tv_cache import TVCache
from sickrage.core.classes import NZBSearchResult, SearchResult, TorrentSearchResult
from sickrage.core.common import MULTI_EP_RESULT, Quality, SEASON_RESULT, cpu_presets
from sickrage.core.databases.main import MainDB
from sickrage.core.helpers import chmod_as_parent, sanitize_file_name, clean_url, bs4_parser, \
    validate_url, try_int, convert_size
from sickrage.core.helpers.show_names import all_possible_show_names
from sickrage.core.nameparser import InvalidNameException, InvalidShowException, NameParser
from sickrage.core.scene_exceptions import get_scene_exceptions
from sickrage.core.tv.show.helpers import find_show
from sickrage.core.websession import WebSession


class GenericProvider(object):
    def __init__(self, name, url, private):
        self.name = name
        self.urls = {'base_url': url}
        self.private = private
        self.supports_backlog = True
        self.supports_absolute_numbering = False
        self.anime_only = False
        self.search_mode = 'eponly'
        self.search_fallback = False
        self.enabled = False
        self.enable_daily = True
        self.enable_backlog = True
        self.cache = TVCache(self)
        self.proper_strings = ['PROPER|REPACK|REAL|RERIP']
        self.search_separator = ' '

        # cookies
        self.enable_cookies = False
        self.cookies = ''

        # web session
        self.session = WebSession(cloudflare=True)

    @property
    def id(self):
        return str(re.sub(r"[^\w\d_]", "_", self.name.strip().lower()))

    @property
    def isEnabled(self):
        return self.enabled

    @property
    def imageName(self):
        return ""

    @property
    def seed_ratio(self):
        return ''

    @property
    def isAlive(self):
        return True

    def _check_auth(self):
        return True

    def login(self):
        return True

    @classmethod
    def get_subclasses(cls):
        yield cls
        if cls.__subclasses__():
            for sub in cls.__subclasses__():
                for s in sub.get_subclasses():
                    yield s

    def getResult(self, season=None, episodes=None):
        """
        Returns a result of the correct type for this provider
        """
        return SearchResult(season, episodes)

    def get_content(self, url):
        if self.login():
            headers = {}
            if url.startswith('http'):
                headers = {'Referer': '/'.join(url.split('/')[:3]) + '/'}

            if not url.startswith('magnet'):
                try:
                    return self.session.get(url, verify=False, headers=headers).content
                except Exception:
                    pass

    def make_filename(self, name):
        return ""

    def get_quality(self, item, anime=False):
        """
        Figures out the quality of the given RSS item node

        item: An elementtree.ElementTree element representing the <item> tag of the RSS feed

        Returns a Quality value obtained from the node's data
        """
        (title, url) = self._get_title_and_url(item)
        quality = Quality.scene_quality(title, anime)
        return quality

    def search(self, search_strings, age=0, show_id=None, season=None, episode=None, **kwargs):
        return []

    @MainDB.with_session
    def _get_season_search_strings(self, show_id, season, episode, session=None):
        """
        Get season search strings.
        """

        search_string = {
            'Season': []
        }

        show_object = find_show(show_id, session=session)
        episode_object = show_object.get_episode(season, episode)

        for show_name in all_possible_show_names(show_id, episode_object.scene_season):
            episode_string = "{}{}".format(show_name, self.search_separator)

            if show_object.air_by_date or show_object.sports:
                episode_string += str(episode_object.airdate).split('-')[0]
            elif show_object.anime:
                episode_string += 'Season'
            else:
                episode_string += 'S{season:0>2}'.format(season=episode_object.scene_season)

            search_string['Season'].append(episode_string.strip())

        return [search_string]

    @MainDB.with_session
    def _get_episode_search_strings(self, show_id, season, episode, add_string='', session=None):
        """
        Get episode search strings.
        """

        search_string = {
            'Episode': []
        }

        show_object = find_show(show_id, session=session)
        episode_object = show_object.get_episode(season, episode)

        for show_name in all_possible_show_names(show_id, episode_object.scene_season):
            episode_string = "{}{}".format(show_name, self.search_separator)
            episode_string_fallback = None

            if show_object.air_by_date:
                episode_string += str(episode_object.airdate).replace('-', ' ')
            elif show_object.sports:
                episode_string += str(episode_object.airdate).replace('-', ' ')
                episode_string += ('|', ' ')[len(self.proper_strings) > 1]
                episode_string += episode_object.airdate.strftime('%b')
            elif show_object.anime:
                # If the show name is a season scene exception, we want to use the indexer episode number.
                if (episode_object.scene_season > 1 and show_name in get_scene_exceptions(show_object.indexer_id, episode_object.scene_season)):
                    # This is apparently a season exception, let's use the scene_episode instead of absolute
                    ep = episode_object.scene_episode
                else:
                    ep = episode_object.scene_absolute_number
                episode_string += '{episode:0>2}'.format(episode=ep)
                episode_string_fallback = episode_string + '{episode:0>3}'.format(episode=ep)
            else:
                episode_string += sickrage.app.naming_ep_type[2] % {
                    'seasonnumber': episode_object.scene_season,
                    'episodenumber': episode_object.scene_episode,
                }

            if add_string:
                episode_string += self.search_separator + add_string
                episode_string_fallback += self.search_separator + add_string

            search_string['Episode'].append(episode_string.strip())
            if episode_string_fallback:
                search_string['Episode'].append(episode_string_fallback.strip())

        return [search_string]

    def _get_title_and_url(self, item):
        """
        Retrieves the title and URL data from the item XML node

        item: An elementtree.ElementTree element representing the <item> tag of the RSS feed

        Returns: A tuple containing two strings representing title and URL respectively
        """

        title = item.get('title', '').replace(' ', '.')
        url = item.get('link', '').replace('&amp;', '&').replace('%26tr%3D', '&tr=')

        return title, url

    def _get_size(self, item):
        """Gets the size from the item"""
        sickrage.app.log.debug("Provider type doesn't have ability to provide download size implemented yet")
        return -1

    def _get_result_stats(self, item):
        # Get seeders/leechers stats
        seeders = item.get('seeders', -1)
        leechers = item.get('leechers', -1)
        return try_int(seeders, -1), try_int(leechers, -1)

    @MainDB.with_session
    def find_search_results(self, show_id, season, episode, search_mode, manualSearch=False, downCurQuality=False, cacheOnly=False, session=None):
        provider_results = {}
        item_list = []

        if not self._check_auth:
            return provider_results

        show_object = find_show(show_id, session=session)
        episode_object = show_object.get_episode(season, episode)

        # search cache for episode result
        provider_results = self.cache.search_cache(show_id, season, episode, manualSearch, downCurQuality)

        # check if this is a cache only search
        if cacheOnly:
            return provider_results

        search_strings = []
        if search_mode == 'sponly':
            # get season search results
            search_strings = self._get_season_search_strings(show_id, season, episode)
        elif search_mode == 'eponly':
            # get single episode search results
            search_strings = self._get_episode_search_strings(show_id, season, episode)

        for curString in search_strings:
            try:
                item_list += self.search(curString, show_id=show_id, season=season, episode=episode)
            except SAXParseException:
                continue

        # sort list by quality
        if item_list:
            # categorize the items into lists by quality
            items = defaultdict(list)
            for item in item_list:
                items[self.get_quality(item, anime=show_object.is_anime)].append(item)

            # temporarily remove the list of items with unknown quality
            unknown_items = items.pop(Quality.UNKNOWN, [])

            # make a generator to sort the remaining items by descending quality
            items_list = (items[quality] for quality in sorted(items, reverse=True))

            # unpack all of the quality lists into a single sorted list
            items_list = list(itertools.chain(*items_list))

            # extend the list with the unknown qualities, now sorted at the bottom of the list
            items_list.extend(unknown_items)

        # filter results
        for item in item_list:
            provider_result = self.getResult()

            provider_result.name, provider_result.url = self._get_title_and_url(item)

            # ignore invalid urls
            if not validate_url(provider_result.url) and not provider_result.url.startswith('magnet'):
                continue

            try:
                parse_result = NameParser(show_id=show_id).parse(provider_result.name)
            except (InvalidNameException, InvalidShowException) as e:
                sickrage.app.log.debug("{}".format(e))
                continue

            provider_result.show_id = parse_result.indexer_id
            provider_result.quality = parse_result.quality
            provider_result.release_group = parse_result.release_group
            provider_result.version = parse_result.version
            provider_result.size = self._get_size(item)
            provider_result.seeders, provider_result.leechers = self._get_result_stats(item)

            sickrage.app.log.debug("Adding item from search to cache: {}".format(provider_result.name))
            self.cache.add_cache_entry(provider_result.name, provider_result.url, provider_result.seeders, provider_result.leechers, provider_result.size)

            if not provider_result.show_id:
                continue

            provider_result_show_obj = find_show(provider_result.show_id, session=session)
            if not provider_result_show_obj:
                continue

            if not parse_result.is_air_by_date and (provider_result_show_obj.air_by_date or provider_result_show_obj.sports):
                sickrage.app.log.debug("This is supposed to be a date search but the result {} didn't parse as one, skipping it".format(provider_result.name))
                continue

            if search_mode == 'sponly':
                if len(parse_result.episode_numbers):
                    sickrage.app.log.debug("This is supposed to be a season pack search but the result {} is not "
                                           "a valid season pack, skipping it".format(provider_result.name))
                    continue
                elif parse_result.season_number != (episode_object.season, episode_object.scene_season)[show_object.is_scene]:
                    sickrage.app.log.debug("This season result {} is for a season we are not searching for, skipping it".format(provider_result.name))
                    continue
            else:
                if not all([parse_result.season_number is not None, parse_result.episode_numbers,
                            parse_result.season_number == (episode_object.season, episode_object.scene_season)[show_object.is_scene],
                            (episode_object.episode, episode_object.scene_episode)[show_object.is_scene] in parse_result.episode_numbers]):
                    sickrage.app.log.debug("The result {} doesn't seem to be a valid episode "
                                           "that we are trying to snatch, ignoring".format(provider_result.name))
                    continue

            provider_result.season = int(parse_result.season_number)
            provider_result.episodes = list(map(int, parse_result.episode_numbers))

            # make sure we want the episode
            for episode_number in provider_result.episodes.copy():
                if not provider_result_show_obj.want_episode(provider_result.season, episode_number, provider_result.quality, manualSearch, downCurQuality):
                    sickrage.app.log.info("RESULT:[{}] QUALITY:[{}] IGNORED!".format(provider_result.name, Quality.qualityStrings[provider_result.quality]))
                    if episode_number in provider_result.episodes:
                        provider_result.episodes.remove(episode_number)

            # detects if season pack and if not checks if we wanted any of the episodes
            if len(provider_result.episodes) != len(parse_result.episode_numbers):
                continue

            sickrage.app.log.debug(
                "FOUND RESULT:[{}] QUALITY:[{}] URL:[{}]".format(provider_result.name, Quality.qualityStrings[provider_result.quality], provider_result.url)
            )

            if len(provider_result.episodes) == 1:
                episode_number = provider_result.episodes[0]
                sickrage.app.log.debug("Single episode result.")
            elif len(provider_result.episodes) > 1:
                episode_number = MULTI_EP_RESULT
                sickrage.app.log.debug("Separating multi-episode result to check for later - result contains episodes: " + str(parse_result.episode_numbers))
            else:
                episode_number = SEASON_RESULT
                sickrage.app.log.debug("Separating full season result to check for later")

            if episode_number not in provider_results:
                provider_results[int(episode_number)] = [provider_result]
            else:
                provider_results[int(episode_number)] += [provider_result]

        return provider_results

    def find_propers(self, show_id, season, episode):
        results = []

        for term in self.proper_strings:
            search_strngs = self._get_episode_search_strings(show_id, season, episode, add_string=term)
            for item in self.search(search_strngs[0], show_id=show_id, season=season, episode=episode):
                result = self.getResult(season, [episode])
                result.name, result.url = self._get_title_and_url(item)
                if not validate_url(result.url) and not result.url.startswith('magnet'):
                    continue

                result.seeders, result.leechers = self._get_result_stats(item)
                result.size = self._get_size(item)
                result.date = datetime.datetime.today()
                results.append(result)

        return results

    def add_cookies_from_ui(self):
        """
        Add the cookies configured from UI to the providers requests session.
        :return: dict
        """

        if isinstance(self, TorrentRssProvider) and not self.cookies:
            return {'result': True,
                    'message': 'This is a TorrentRss provider without any cookies provided. '
                               'Cookies for this provider are considered optional.'}

        # This is the generic attribute used to manually add cookies for provider authentication
        if not self.enable_cookies:
            return {'result': False,
                    'message': 'Adding cookies is not supported for provider: {}'.format(self.name)}

        if not self.cookies:
            return {'result': False,
                    'message': 'No Cookies added from ui for provider: {}'.format(self.name)}

        cookie_validator = re.compile(r'^([\w%]+=[\w%]+)(;[\w%]+=[\w%]+)*$')
        if not cookie_validator.match(self.cookies):
            sickrage.app.alerts.message(
                'Failed to validate cookie for provider {}'.format(self.name),
                'Cookie is not correctly formatted: {}'.format(self.cookies))

            return {'result': False,
                    'message': 'Cookie is not correctly formatted: {}'.format(self.cookies)}

        if hasattr(self, 'required_cookies') and not all(
                req_cookie in [x.rsplit('=', 1)[0] for x in self.cookies.split(';')] for req_cookie in
                self.required_cookies):
            return {'result': False,
                    'message': "You haven't configured the required cookies. Please login at {provider_url}, "
                               "and make sure you have copied the following cookies: {required_cookies!r}"
                        .format(provider_url=self.name, required_cookies=self.required_cookies)}

        # cookie_validator got at least one cookie key/value pair, let's return success
        add_dict_to_cookiejar(self.session.cookies, dict(x.rsplit('=', 1) for x in self.cookies.split(';')))

        return {'result': True,
                'message': ''}

    def check_required_cookies(self):
        """
        Check if we have the required cookies in the requests sessions object.

        Meaning that we've already successfully authenticated once, and we don't need to go through this again.
        Note! This doesn't mean the cookies are correct!
        """
        if hasattr(self, 'required_cookies'):
            return all(dict_from_cookiejar(self.session.cookies).get(cookie) for cookie in self.required_cookies)

        # A reminder for the developer, implementing cookie based authentication.
        sickrage.app.log.error(
            'You need to configure the required_cookies attribute, for the provider: {}'.format(self.name))

    def cookie_login(self, check_login_text, check_url=None):
        """
        Check the response for text that indicates a login prompt.

        In that case, the cookie authentication was not successful.
        :param check_login_text: A string that's visible when the authentication failed.
        :param check_url: The url to use to test the login with cookies. By default the providers home page is used.

        :return: False when authentication was not successful. True if successful.
        """
        check_url = check_url or self.urls['base_url']

        if self.check_required_cookies():
            # All required cookies have been found within the current session, we don't need to go through this again.
            return True

        if self.cookies:
            result = self.add_cookies_from_ui()
            if not result['result']:
                sickrage.app.alerts.message(result['message'])
                sickrage.app.log.warning(result['message'])
                return False
        else:
            sickrage.app.log.warning('Failed to login, you will need to add your cookies in the provider '
                                     'settings')

            sickrage.app.alerts.error(
                'Failed to auth with {provider}'.format(provider=self.name),
                'You will need to add your cookies in the provider settings')
            return False

        response = self.session.get(check_url)
        if any([not response, not (response.text and response.status_code == 200),
                check_login_text.lower() in response.text.lower()]):
            sickrage.app.log.warning('Please configure the required cookies for this provider. Check your '
                                     'provider settings')

            sickrage.app.alerts.error(
                'Wrong cookies for {}'.format(self.name),
                'Check your provider settings'
            )
            self.session.cookies.clear()
            return False
        else:
            return True

    @classmethod
    def getDefaultProviders(cls):
        pass

    @classmethod
    def getProvider(cls, name):
        providerMatch = [x for x in cls.get_providers() if x.name == name]
        if len(providerMatch) == 1:
            return providerMatch[0]

    @classmethod
    def getProviderByID(cls, id):
        providerMatch = [x for x in cls.get_providers() if x.id == id]
        if len(providerMatch) == 1:
            return providerMatch[0]

    @classmethod
    def get_providers(cls):
        modules = [TorrentProvider.type, NZBProvider.type]
        for type in []:
            modules += cls.load_providers(type)
        return modules

    @classmethod
    def load_providers(cls, provider_type):
        providers = []

        for (__, name, __) in pkgutil.iter_modules([os.path.join(os.path.dirname(__file__), provider_type)]):
            imported_module = importlib.import_module('.{}.{}'.format(provider_type, name), package='sickrage.providers')
            for __, klass in inspect.getmembers(imported_module, predicate=lambda o: all([inspect.isclass(o) and issubclass(o, GenericProvider),
                                                                                          o is not NZBProvider, o is not TorrentProvider,
                                                                                          getattr(o, 'type', None) == provider_type])):
                providers += [klass()]
                break

        return providers


class TorrentProvider(GenericProvider):
    type = 'torrent'

    def __init__(self, name, url, private):
        super(TorrentProvider, self).__init__(name, url, private)
        self.ratio = None

    @property
    def isActive(self):
        return sickrage.app.config.use_torrents and self.isEnabled

    @property
    def imageName(self):
        return self.id

    @property
    def seed_ratio(self):
        """
        Provider should override this value if custom seed ratio enabled
        It should return the value of the provider seed ratio
        """
        return self.ratio

    def getResult(self, season=None, episodes=None):
        """
        Returns a result of the correct type for this provider
        """
        result = TorrentSearchResult(season, episodes)
        result.provider = self
        return result

    def get_content(self, url):
        result = None

        def verify_torrent(content):
            try:
                if bdecode(content).get('info'):
                    return content
            except Exception:
                pass

        if url.startswith('magnet'):
            # try iTorrents
            info_hash = str(re.findall(r'urn:btih:([\w]{32,40})', url)[0]).upper()
            if len(info_hash) == 32:
                info_hash = b16encode(b32decode(info_hash)).upper()

            if info_hash:
                try:
                    # add to external api database
                    TorrentCacheAPI().add(url)
                    result = verify_torrent(b64decode(TorrentCacheAPI().get(info_hash)['data']['content']).strip())
                except Exception:
                    torrent_url = "https://itorrents.org/torrent/{info_hash}.torrent".format(info_hash=info_hash)
                    result = verify_torrent(super(TorrentProvider, self).get_content(torrent_url))

        if not result:
            result = verify_torrent(super(TorrentProvider, self).get_content(url))

        return result

    def _get_title_and_url(self, item):
        title, download_url = '', ''
        if isinstance(item, (dict, FeedParserDict)):
            title = item.get('title', '')
            download_url = item.get('url', item.get('link', ''))
        elif isinstance(item, (list, tuple)) and len(item) > 1:
            title = item[0]
            download_url = item[1]

        # Temp global block `DIAMOND` releases
        if title.endswith('DIAMOND'):
            sickrage.app.log.info('Skipping DIAMOND release for mass fake releases.')
            title = download_url = 'FAKERELEASE'
        else:
            title = self._clean_title_from_provider(title)

        download_url = download_url.replace('&amp;', '&')

        return title, download_url

    def _get_size(self, item):
        return item.get('size', -1)

    def download_result(self, result):
        """
        Downloads a result to the appropriate black hole folder.

        :param result: SearchResult instance to download.
        :return: boolean, True on success
        """

        if not result.content:
            return False

        filename = self.make_filename(result.name)

        sickrage.app.log.info("Saving TORRENT to " + filename)

        # write content to torrent file
        with open(filename, 'wb') as f:
            f.write(result.content)

        return True

    @staticmethod
    def _clean_title_from_provider(title):
        return (title or '').replace(' ', '.')

    def make_filename(self, name):
        return os.path.join(sickrage.app.config.torrent_dir, '{}.torrent'.format(sanitize_file_name(name)))

    def add_trackers(self, result):
        """
        Adds public trackers to either torrent file or magnet link
        :param result: SearchResult
        :return: SearchResult
        """

        try:
            trackers_list = self.session.get('https://cdn.sickrage.ca/torrent_trackers/').text.split()
        except Exception:
            trackers_list = []

        if trackers_list:
            # adds public torrent trackers to magnet url
            if result.url.startswith('magnet:'):
                if not result.url.endswith('&tr='):
                    result.url += '&tr='
                result.url += '&tr='.join(trackers_list)

            # adds public torrent trackers to content
            if result.content:
                decoded_data = bdecode(result.content)
                if not decoded_data.get('announce-list'):
                    decoded_data['announce-list'] = []

                for tracker in trackers_list:
                    if tracker not in decoded_data['announce-list']:
                        decoded_data['announce-list'].append([str(tracker)])
                result.content = bencode(decoded_data)

        return result

    @classmethod
    def get_providers(cls):
        return super(TorrentProvider, cls).load_providers(cls.type)


class NZBProvider(GenericProvider):
    type = 'nzb'

    def __init__(self, name, url, private):
        super(NZBProvider, self).__init__(name, url, private)
        self.api_key = ''
        self.username = ''
        self.torznab = False

    @property
    def isActive(self):
        return sickrage.app.config.use_nzbs and self.isEnabled

    @property
    def imageName(self):
        return self.id

    def getResult(self, season=None, episodes=None):
        """
        Returns a result of the correct type for this provider
        """
        result = NZBSearchResult(season, episodes)
        result.type = ('nzb', 'torznab')[self.torznab]
        result.provider = self
        return result

    def _get_size(self, item):
        return item.get('size', -1)

    def download_result(self, result):
        """
        Downloads a result to the appropriate black hole folder.

        :param result: SearchResult instance to download.
        :return: boolean, True on success
        """

        if not result.content:
            return False

        filename = self.make_filename(result.name)

        # Support for Jackett/TorzNab
        if (result.url.endswith('torrent') or result.url.startswith('magnet')) and self.type in ['nzb', 'newznab']:
            filename = "{}.torrent".format(filename.rsplit('.', 1)[0])

        if result.type == "nzb":
            sickrage.app.log.info("Saving NZB to " + filename)

            # write content to torrent file
            with open(filename, 'wb') as f:
                f.write(result.content)

            return True
        elif result.type == "nzbdata":
            filename = os.path.join(sickrage.app.config.nzb_dir, result.name + ".nzb")

            sickrage.app.log.info("Saving NZB to " + filename)

            # save the data to disk
            try:
                with open(filename, 'w') as fileOut:
                    fileOut.write(result.extraInfo[0])

                chmod_as_parent(filename)

                return True
            except EnvironmentError as e:
                sickrage.app.log.error("Error trying to save NZB to black hole: {}".format(e))

    def make_filename(self, name):
        return os.path.join(sickrage.app.config.nzb_dir, '{}.nzb'.format(sanitize_file_name(name)))

    @classmethod
    def get_providers(cls):
        return super(NZBProvider, cls).load_providers(cls.type)


class TorrentRssProvider(TorrentProvider):
    type = 'torrentrss'

    def __init__(self,
                 name,
                 url,
                 cookies='',
                 titleTAG='title',
                 search_mode='eponly',
                 search_fallback=False,
                 enable_daily=False,
                 enable_backlog=False,
                 default=False, ):
        super(TorrentRssProvider, self).__init__(name, clean_url(url), False)

        self.cache = TorrentRssCache(self)
        self.supports_backlog = False

        self.search_mode = search_mode
        self.search_fallback = search_fallback
        self.enable_daily = enable_daily
        self.enable_backlog = enable_backlog
        self.enable_cookies = True
        self.cookies = cookies
        self.required_cookies = ('uid', 'pass')
        self.titleTAG = titleTAG
        self.default = default

    def _get_title_and_url(self, item):

        title = item.get(self.titleTAG, '')
        title = self._clean_title_from_provider(title)

        attempt_list = [lambda: item.get('torrent_magneturi'),

                        lambda: item.enclosures[0].href,

                        lambda: item.get('link')]

        url = ''
        for cur_attempt in attempt_list:
            try:
                url = cur_attempt()
            except Exception:
                continue

            if title and url:
                break

        return title, url

    def validateRSS(self):
        torrent_file = None

        try:
            add_cookie = self.add_cookies_from_ui()
            if not add_cookie.get('result'):
                return add_cookie

            data = self.cache._get_rss_data()['entries']
            if not data:
                return {'result': False,
                        'message': 'No items found in the RSS feed {}'.format(self.urls['base_url'])}

            (title, url) = self._get_title_and_url(data[0])

            if not title:
                return {'result': False,
                        'message': 'Unable to get title from first item'}

            if not url:
                return {'result': False,
                        'message': 'Unable to get torrent url from first item'}

            if url.startswith('magnet:') and re.search(r'urn:btih:([\w]{32,40})', url):
                return {'result': True,
                        'message': 'RSS feed Parsed correctly'}
            else:
                try:
                    torrent_file = self.session.get(url).content
                    bdecode(torrent_file)
                except Exception as e:
                    if data:
                        self.dumpHTML(torrent_file)
                    return {'result': False,
                            'message': 'Torrent link is not a valid torrent file: {}'.format(e)}

            return {'result': True,
                    'message': 'RSS feed Parsed correctly'}

        except Exception as e:
            return {'result': False,
                    'message': 'Error when trying to load RSS: {}'.format(e)}

    @staticmethod
    def dumpHTML(data):
        dumpName = os.path.join(sickrage.app.cache_dir, 'custom_torrent.html')

        try:
            with open(dumpName, 'wb') as fileOut:
                fileOut.write(data)

            chmod_as_parent(dumpName)

            sickrage.app.log.info("Saved custom_torrent html dump %s " % dumpName)
        except IOError as e:
            sickrage.app.log.error("Unable to save the file: %s " % repr(e))
            return False

        return True

    @classmethod
    def get_providers(cls):
        providers = cls.getDefaultProviders()

        try:
            for curProviderStr in sickrage.app.config.custom_providers.split('!!!'):
                if not len(curProviderStr):
                    continue

                try:
                    cur_type, curProviderData = curProviderStr.split('|', 1)
                    if cur_type == "torrentrss":
                        cur_name, cur_url, cur_cookies, cur_title_tag = curProviderData.split('|')
                        providers += [TorrentRssProvider(cur_name, cur_url, cur_cookies, cur_title_tag)]
                except Exception:
                    continue
        except Exception:
            pass

        return providers

    @classmethod
    def getDefaultProviders(cls):
        return [
            cls('showRSS', 'showrss.info', '', 'title', 'eponly', False, False, False, True)
        ]


class NewznabProvider(NZBProvider):
    type = 'newznab'

    def __init__(self, name, url, key='0', catIDs='5030,5040', search_mode='eponly', search_fallback=False,
                 enable_daily=False, enable_backlog=False, default=False):
        super(NewznabProvider, self).__init__(name, clean_url(url), bool(key != '0'))

        self.key = key

        self.search_mode = search_mode
        self.search_fallback = search_fallback
        self.enable_daily = enable_daily
        self.enable_backlog = enable_backlog

        self.catIDs = catIDs
        self.default = default

        self.caps = False
        self.cap_tv_search = None
        self.force_query = False

        self.cache = TVCache(self, min_time=30)

    def set_caps(self, data):
        """
        Set caps.
        """
        if not data:
            return

        def _parse_cap(tag):
            elm = data.find(tag)
            is_supported = elm and all([elm.get('supportedparams'), elm.get('available') == 'yes'])
            return elm['supportedparams'].split(',') if is_supported else []

        self.cap_tv_search = _parse_cap('tv-search')

        self.caps = any(self.cap_tv_search)

    def get_newznab_categories(self, just_caps=False):
        """
        Use the newznab provider url and apikey to get the capabilities.

        Makes use of the default newznab caps param. e.a. http://yournewznab/api?t=caps&apikey=skdfiw7823sdkdsfjsfk
        Returns a tuple with (succes or not, array with dicts [{'id': '5070', 'name': 'Anime'},
        {'id': '5080', 'name': 'Documentary'}, {'id': '5020', 'name': 'Foreign'}...etc}], error message)
        """
        return_categories = []

        if not self._check_auth():
            return False, return_categories, 'Provider requires auth and your key is not set'

        url_params = {'t': 'caps'}
        if self.private and self.key:
            url_params['apikey'] = self.key

        try:
            response = self.session.get(urljoin(self.urls['base_url'], 'api'), params=url_params).text
        except Exception:
            error_string = 'Error getting caps xml for [{}]'.format(self.name)
            sickrage.app.log.warning(error_string)
            return False, return_categories, error_string

        with bs4_parser(response) as html:
            if not html.find('categories'):
                error_string = 'Error parsing caps xml for [{}]'.format(self.name)
                sickrage.app.log.debug(error_string)
                return False, return_categories, error_string

            self.set_caps(html.find('searching'))
            if just_caps:
                return

            for category in html('category'):
                if 'TV' in category.get('name', '') and category.get('id', ''):
                    return_categories.append({'id': category['id'], 'name': category['name']})
                    for subcat in category('subcat'):
                        if subcat.get('name', '') and subcat.get('id', ''):
                            return_categories.append({'id': subcat['id'], 'name': subcat['name']})

            return True, return_categories, ''

    def _doGeneralSearch(self, search_string):
        return self.search({'q': search_string})

    def _check_auth(self):
        if self.private and not self.key:
            sickrage.app.log.warning('Invalid api key for {}. Check your settings'.format(self.name))
            return False

        return True

    def _check_auth_from_data(self, data):
        """
        Check that the returned data is valid.

        :return: _check_auth if valid otherwise False if there is an error
        """
        if data('categories') + data('item'):
            return self._check_auth()

        try:
            err_desc = data.error.attrs['description']
            if not err_desc:
                raise Exception
        except (AttributeError, TypeError):
            return self._check_auth()

        sickrage.app.log.info(err_desc)

        return False

    @MainDB.with_session
    def search(self, search_strings, age=0, show_id=None, season=None, episode=None, session=None, **kwargs):
        """
        Search indexer using the params in search_strings, either for latest releases, or a string/id search.

        :return: list of results in dict form
        """
        results = []

        if not self._check_auth():
            return results

        # For providers that don't have caps, or for which the t=caps is not working.
        if not self.caps:
            self.get_newznab_categories(just_caps=True)

        show_object = find_show(show_id, session=session)
        episode_object = show_object.get_episode(season, episode)

        for mode in search_strings:
            self.torznab = False
            search_params = {
                't': 'search',
                'limit': 100,
                'offset': 0,
                'cat': self.catIDs.strip(', ') or '5030,5040',
                'maxage': sickrage.app.config.usenet_retention
            }

            if self.private and self.key:
                search_params['apikey'] = self.key

            if mode != 'RSS':
                if (self.cap_tv_search or not self.cap_tv_search == 'True') and not self.force_query:
                    search_params['t'] = 'tvsearch'
                    search_params.update({'tvdbid': show_id})

                if search_params['t'] == 'tvsearch':
                    if show_object.air_by_date or show_object.sports:
                        date_str = str(episode_object.airdate)
                        search_params['season'] = date_str.partition('-')[0]
                        search_params['ep'] = date_str.partition('-')[2].replace('-', '/')
                    else:
                        search_params['season'] = episode_object.scene_season
                        search_params['ep'] = episode_object.scene_episode

                if mode == 'Season':
                    search_params.pop('ep', '')

            sickrage.app.log.debug('Search mode: {0}'.format(mode))

            for search_string in search_strings[mode]:
                if mode != 'RSS':
                    # If its a PROPER search, need to change param to 'search' so it searches using 'q' param
                    if any(proper_string in search_string for proper_string in self.proper_strings):
                        search_params['t'] = 'search'

                    sickrage.app.log.debug("Search string: {}".format(search_string))

                    if search_params['t'] != 'tvsearch':
                        search_params['q'] = search_string

                sleep(cpu_presets[sickrage.app.config.cpu_preset])

                try:
                    data = self.session.get(urljoin(self.urls['base_url'], 'api'), params=search_params).text
                    results += self.parse(data, mode)
                except Exception:
                    sickrage.app.log.debug('No data returned from provider')
                    continue

                # Since we arent using the search string,
                # break out of the search string loop
                if 'tvdbid' in search_params:
                    break

        # Reprocess but now use force_query = True
        if not results and not self.force_query:
            self.force_query = True
            return self.search(search_strings, show_id=show_id, season=season, episode=episode)

        return results

    def parse(self, data, mode, **kwargs):
        results = []

        with bs4_parser(data) as html:
            if not self._check_auth_from_data(html):
                return results

            try:
                self.torznab = 'xmlns:torznab' in html.rss.attrs
            except AttributeError:
                self.torznab = False

            if not html('item'):
                sickrage.app.log.debug('No results returned from provider. Check chosen Newznab '
                                       'search categories in provider settings and/or usenet '
                                       'retention')
                return results

            for item in html('item'):
                try:
                    title = item.title.get_text(strip=True)
                    download_url = None
                    if item.link:
                        url = item.link.get_text(strip=True)
                        if validate_url(url) or url.startswith('magnet'):
                            download_url = url

                        if not download_url:
                            url = item.link.next.strip()
                            if validate_url(url) or url.startswith('magnet'):
                                download_url = url

                    if not download_url and item.enclosure:
                        url = item.enclosure.get('url', '').strip()
                        if validate_url(url) or url.startswith('magnet'):
                            download_url = url

                    if not (title and download_url):
                        continue

                    seeders = leechers = -1
                    if 'gingadaddy' in self.urls['base_url']:
                        size_regex = re.search(r'\d*.?\d* [KMGT]B', str(item.description))
                        item_size = size_regex.group() if size_regex else -1
                    else:
                        item_size = item.size.get_text(strip=True) if item.size else -1

                        newznab_attrs = item(re.compile('newznab:attr'))
                        torznab_attrs = item(re.compile('torznab:attr'))
                        for attr in newznab_attrs + torznab_attrs:
                            item_size = attr['value'] if attr['name'] == 'size' else item_size
                            seeders = try_int(attr['value']) if attr['name'] == 'seeders' else seeders
                            peers = try_int(attr['value']) if attr['name'] == 'peers' else None
                            leechers = peers - seeders if peers else leechers

                    if not item_size or (self.torznab and (seeders is -1 or leechers is -1)):
                        continue

                    size = convert_size(item_size, -1)

                    results += [
                        {'title': title, 'link': download_url, 'size': size, 'seeders': seeders, 'leechers': leechers}
                    ]

                    if mode != 'RSS':
                        sickrage.app.log.debug('Found result: {}'.format(title))
                except (AttributeError, TypeError, KeyError, ValueError, IndexError):
                    sickrage.app.log.error('Failed parsing provider')

        return results

    @classmethod
    def get_providers(cls):
        providers = cls.getDefaultProviders()

        try:
            for curProviderStr in sickrage.app.config.custom_providers.split('!!!'):
                if not len(curProviderStr):
                    continue

                try:
                    cur_type, curProviderData = curProviderStr.split('|', 1)

                    if cur_type == "newznab":
                        cur_name, cur_url, cur_key, cur_cat = curProviderData.split('|')
                        cur_url = clean_url(cur_url)

                        provider = NewznabProvider(
                            cur_name,
                            cur_url,
                            cur_key,
                            cur_cat
                        )

                        providers += [provider]
                except Exception:
                    continue
        except Exception:
            pass

        return providers

    @classmethod
    def getDefaultProviders(cls):
        return [
            cls('DOGnzb', 'https://api.dognzb.cr', '', '5030,5040,5060,5070', 'eponly', False, False, False, True),
            cls('NZB.Cat', 'https://nzb.cat', '', '5030,5040,5010', 'eponly', True, True, True, True),
            cls('NZBGeek', 'https://api.nzbgeek.info', '', '5030,5040', 'eponly', False, False, False, True),
            cls('NZBs.org', 'https://nzbs.org', '', '5030,5040', 'eponly', False, False, False, True),
            cls('Usenet-Crawler', 'https://www.usenet-crawler.com', '', '5030,5040', 'eponly', False, False, False,
                True)
        ]


class TorrentRssCache(TVCache):
    def __init__(self, provider_obj):
        TVCache.__init__(self, provider_obj)
        self.min_time = 15

    def _get_rss_data(self):
        sickrage.app.log.debug("Cache update URL: %s" % self.provider.urls['base_url'])

        if self.provider.cookies:
            add_dict_to_cookiejar(self.provider.session.cookies,
                                  dict(x.rsplit('=', 1) for x in self.provider.cookies.split(';')))

        return self.get_rss_feed(self.provider.urls['base_url'])


class SearchProviders(dict):
    def __init__(self):
        super(SearchProviders, self).__init__()

        self.provider_order = []

        self[NZBProvider.type] = {}
        self[TorrentProvider.type] = {}
        self[NewznabProvider.type] = {}
        self[TorrentRssProvider.type] = {}

    def load(self):
        self[NZBProvider.type] = dict([(p.id, p) for p in NZBProvider.get_providers()])
        self[TorrentProvider.type] = dict([(p.id, p) for p in TorrentProvider.get_providers()])
        self[NewznabProvider.type] = dict([(p.id, p) for p in NewznabProvider.get_providers()])
        self[TorrentRssProvider.type] = dict([(p.id, p) for p in TorrentRssProvider.get_providers()])

    def sort(self, key=None, randomize=False):
        sorted_providers = []

        self.provider_order = [x for x in self.provider_order if x in self.all().keys()]
        self.provider_order += [x for x in self.all().keys() if x not in self.provider_order]

        if not key:
            key = self.provider_order

        if randomize:
            random.shuffle(key)

        for p in [self.enabled()[x] for x in key if x in self.enabled()]:
            sorted_providers.append(p)

        for p in [self.disabled()[x] for x in key if x in self.disabled()]:
            sorted_providers.append(p)

        return OrderedDict([(x.id, x) for x in sorted_providers])

    def enabled(self):
        return dict([(pID, pObj) for pID, pObj in self.all().items() if pObj.isEnabled])

    def disabled(self):
        return dict([(pID, pObj) for pID, pObj in self.all().items() if not pObj.isEnabled])

    def all(self):
        return {**self.nzb(), **self.torrent(), **self.newznab(), **self.torrentrss()}

    def all_nzb(self):
        return {**self.nzb(), **self.newznab()}

    def all_torrent(self):
        return {**self.torrent(), **self.torrentrss()}

    def nzb(self):
        return self[NZBProvider.type]

    def torrent(self):
        return self[TorrentProvider.type]

    def newznab(self):
        return self[NewznabProvider.type]

    def torrentrss(self):
        return self[TorrentRssProvider.type]
