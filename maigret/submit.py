import asyncio
import json
import re
from typing import List, Tuple
import xml.etree.ElementTree as ET
from aiohttp import TCPConnector, ClientSession
import requests
import cloudscraper

from .activation import import_aiohttp_cookies
from .checking import maigret
from .result import QueryStatus
from .settings import Settings
from .sites import MaigretDatabase, MaigretSite, MaigretEngine
from .utils import get_random_user_agent, get_match_ratio


class CloudflareSession:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper()

    async def get(self, *args, **kwargs):
        await asyncio.sleep(0)
        res = self.scraper.get(*args, **kwargs)
        self.last_text = res.text
        self.status = res.status_code
        return self

    def status_code(self):
        return self.status

    async def text(self):
        await asyncio.sleep(0)
        return self.last_text

    async def close(self):
        pass


class Submitter:
    HEADERS = {
        "User-Agent": get_random_user_agent(),
    }

    SEPARATORS = "\"'"

    RATIO = 0.6
    TOP_FEATURES = 5
    URL_RE = re.compile(r"https?://(www\.)?")

    def __init__(self, db: MaigretDatabase, settings: Settings, logger, args):
        self.settings = settings
        self.args = args
        self.db = db
        self.logger = logger

        from aiohttp_socks import ProxyConnector

        proxy = self.args.proxy
        cookie_jar = None
        if args.cookie_file:
            cookie_jar = import_aiohttp_cookies(args.cookie_file)

        connector = ProxyConnector.from_url(proxy) if proxy else TCPConnector(ssl=False)
        connector.verify_ssl = False
        self.session = ClientSession(
            connector=connector, trust_env=True, cookie_jar=cookie_jar
        )

    @staticmethod
    def get_alexa_rank(site_url_main):
        url = f"http://data.alexa.com/data?cli=10&url={site_url_main}"
        xml_data = requests.get(url).text
        root = ET.fromstring(xml_data)
        alexa_rank = 0

        try:
            alexa_rank = int(root.find('.//REACH').attrib['RANK'])
        except Exception:
            pass

        return alexa_rank

    @staticmethod
    def extract_mainpage_url(url):
        return "/".join(url.split("/", 3)[:3])

    async def site_self_check(self, site, semaphore, silent=False):
        changes = {
            "disabled": False,
        }

        check_data = [
            (site.username_claimed, QueryStatus.CLAIMED),
            (site.username_unclaimed, QueryStatus.AVAILABLE),
        ]

        self.logger.info(f"Checking {site.name}...")

        for username, status in check_data:
            results_dict = await maigret(
                username=username,
                site_dict={site.name: site},
                proxy=self.args.proxy,
                logger=self.logger,
                cookies=self.args.cookie_file,
                timeout=30,
                id_type=site.type,
                forced=True,
                no_progressbar=True,
            )

            # don't disable entries with other ids types
            # TODO: make normal checking
            if site.name not in results_dict:
                self.logger.info(results_dict)
                changes["disabled"] = True
                continue

            result = results_dict[site.name]["status"]

            site_status = result.status

            if site_status != status:
                if site_status == QueryStatus.UNKNOWN:
                    msgs = site.absence_strs
                    etype = site.check_type
                    self.logger.warning(
                        "Error while searching '%s' in %s: %s, %s, check type %s",
                        username,
                        site.name,
                        result.context,
                        msgs,
                        etype,
                    )
                    # don't disable in case of available username
                    if status == QueryStatus.CLAIMED:
                        changes["disabled"] = True
                elif status == QueryStatus.CLAIMED:
                    self.logger.warning(
                        f"Not found `{username}` in {site.name}, must be claimed"
                    )
                    self.logger.info(results_dict[site.name])
                    changes["disabled"] = True
                else:
                    self.logger.warning(
                        f"Found `{username}` in {site.name}, must be available"
                    )
                    self.logger.info(results_dict[site.name])
                    changes["disabled"] = True

        self.logger.info(f"Site {site.name} checking is finished")

        return changes

    def generate_additional_fields_dialog(self, engine: MaigretEngine, dialog):
        fields = {}
        if 'urlSubpath' in engine.site.get('url', ''):
            msg = (
                'Detected engine suppose additional URL subpath using (/forum/, /blog/, etc). '
                'Enter in manually if it exists: '
            )
            subpath = input(msg).strip('/')
            if subpath:
                fields['urlSubpath'] = f'/{subpath}'
        return fields

    async def detect_known_engine(
        self, url_exists, url_mainpage
    ) -> [List[MaigretSite], str]:
        resp_text = ''
        try:
            r = await self.session.get(url_mainpage)
            content = await r.content.read()
            charset = r.charset or "utf-8"
            resp_text = content.decode(charset, "ignore")
            self.logger.debug(resp_text)
        except Exception as e:
            self.logger.warning(e)
            print("Some error while checking main page")
            return [], resp_text

        for engine in self.db.engines:
            strs_to_check = engine.__dict__.get("presenseStrs")
            if strs_to_check and resp_text:
                all_strs_in_response = True
                for s in strs_to_check:
                    if s not in resp_text:
                        all_strs_in_response = False
                sites = []
                if all_strs_in_response:
                    engine_name = engine.__dict__.get("name")

                    print(f"Detected engine {engine_name} for site {url_mainpage}")

                    usernames_to_check = self.settings.supposed_usernames
                    supposed_username = self.extract_username_dialog(url_exists)
                    if supposed_username:
                        usernames_to_check = [supposed_username] + usernames_to_check

                    add_fields = self.generate_additional_fields_dialog(
                        engine, url_exists
                    )

                    for u in usernames_to_check:
                        site_data = {
                            "urlMain": url_mainpage,
                            "name": url_mainpage.split("//")[1],
                            "engine": engine_name,
                            "usernameClaimed": u,
                            "usernameUnclaimed": "noonewouldeverusethis7",
                            **add_fields,
                        }
                        self.logger.info(site_data)

                        maigret_site = MaigretSite(
                            url_mainpage.split("/")[-1], site_data
                        )
                        maigret_site.update_from_engine(
                            self.db.engines_dict[engine_name]
                        )
                        sites.append(maigret_site)

                    return sites, resp_text

        return [], resp_text

    def extract_username_dialog(self, url):
        url_parts = url.rstrip("/").split("/")
        supposed_username = url_parts[-1].strip('@')
        entered_username = input(
            f'Is "{supposed_username}" a valid username? If not, write it manually: '
        )
        return entered_username if entered_username else supposed_username

    async def check_features_manually(
        self, url_exists, url_mainpage, cookie_file, redirects=False
    ):
        custom_headers = {}
        while self.args.verbose:
            header_key = input(
                'Specify custom header if you need or just press Enter to skip. Header name: '
            )
            if not header_key:
                break
            header_value = input('Header value: ')
            custom_headers[header_key.strip()] = header_value.strip()

        supposed_username = self.extract_username_dialog(url_exists)
        non_exist_username = "noonewouldeverusethis7"

        url_user = url_exists.replace(supposed_username, "{username}")
        url_not_exists = url_exists.replace(supposed_username, non_exist_username)

        headers = dict(self.HEADERS)
        headers.update(custom_headers)

        exists_resp = await self.session.get(
            url_exists,
            headers=headers,
            allow_redirects=redirects,
        )
        exists_resp_text = await exists_resp.text()
        self.logger.debug(url_exists)
        self.logger.debug(exists_resp.status)
        self.logger.debug(exists_resp_text)

        non_exists_resp = await self.session.get(
            url_not_exists,
            headers=headers,
            allow_redirects=redirects,
        )
        non_exists_resp_text = await non_exists_resp.text()
        self.logger.debug(url_not_exists)
        self.logger.debug(non_exists_resp.status)
        self.logger.debug(non_exists_resp_text)

        a = exists_resp_text
        b = non_exists_resp_text

        tokens_a = set(re.split(f'[{self.SEPARATORS}]', a))
        tokens_b = set(re.split(f'[{self.SEPARATORS}]', b))

        a_minus_b = tokens_a.difference(tokens_b)
        b_minus_a = tokens_b.difference(tokens_a)

        if len(a_minus_b) == len(b_minus_a) == 0:
            print("The pages for existing and non-existing account are the same!")

        top_features_count = int(
            input(
                f"Specify count of features to extract [default {self.TOP_FEATURES}]: "
            )
            or self.TOP_FEATURES
        )

        match_fun = get_match_ratio(self.settings.presence_strings)

        presence_list = sorted(a_minus_b, key=match_fun, reverse=True)[
            :top_features_count
        ]

        print("Detected text features of existing account: " + ", ".join(presence_list))
        features = input("If features was not detected correctly, write it manually: ")

        if features:
            presence_list = list(map(str.strip, features.split(",")))

        absence_list = sorted(b_minus_a, key=match_fun, reverse=True)[
            :top_features_count
        ]
        print(
            "Detected text features of non-existing account: " + ", ".join(absence_list)
        )
        features = input("If features was not detected correctly, write it manually: ")

        if features:
            absence_list = list(map(str.strip, features.split(",")))

        site_data = {
            "absenceStrs": absence_list,
            "presenseStrs": presence_list,
            "url": url_user,
            "urlMain": url_mainpage,
            "usernameClaimed": supposed_username,
            "usernameUnclaimed": non_exist_username,
            "checkType": "message",
        }

        if headers != self.HEADERS:
            site_data['headers'] = headers

        site = MaigretSite(url_mainpage.split("/")[-1], site_data)
        return site

    async def dialog(self, url_exists, cookie_file):
        domain_raw = self.URL_RE.sub("", url_exists).strip().strip("/")
        domain_raw = domain_raw.split("/")[0]
        self.logger.info('Domain is %s', domain_raw)

        # check for existence
        matched_sites = list(
            filter(lambda x: domain_raw in x.url_main + x.url, self.db.sites)
        )

        if matched_sites:
            print(
                f'Sites with domain "{domain_raw}" already exists in the Maigret database!'
            )
            status = lambda s: "(disabled)" if s.disabled else ""
            url_block = lambda s: f"\n\t{s.url_main}\n\t{s.url}"
            print(
                "\n".join(
                    [
                        f"{site.name} {status(site)}{url_block(site)}"
                        for site in matched_sites
                    ]
                )
            )

            if input("Do you want to continue? [yN] ").lower() in "n":
                return False

        url_mainpage = self.extract_mainpage_url(url_exists)

        print('Detecting site engine, please wait...')
        sites = []
        try:
            sites, text = await self.detect_known_engine(url_exists, url_exists)
        except KeyboardInterrupt:
            print('Engine detect process is interrupted.')

        if 'cloudflare' in text.lower():
            print(
                'Cloudflare protection detected. I will use cloudscraper for futher work'
            )
            # self.session = CloudflareSession()

        if not sites:
            print("Unable to detect site engine, lets generate checking features")

            redirects = False
            if self.args.verbose:
                redirects = (
                    'y' in input('Should we do redirects automatically? [yN] ').lower()
                )

            sites = [
                await self.check_features_manually(
                    url_exists,
                    url_mainpage,
                    cookie_file,
                    redirects,
                )
            ]

        self.logger.debug(sites[0].__dict__)

        sem = asyncio.Semaphore(1)

        print("Checking, please wait...")
        found = False
        chosen_site = None
        for s in sites:
            chosen_site = s
            result = await self.site_self_check(s, sem)
            if not result["disabled"]:
                found = True
                break

        if not found:
            print(
                f"Sorry, we couldn't find params to detect account presence/absence in {chosen_site.name}."
            )
            print(
                "Try to run this mode again and increase features count or choose others."
            )
            self.logger.debug(json.dumps(chosen_site.json))
            return False
        else:
            if (
                input(
                    f"Site {chosen_site.name} successfully checked. Do you want to save it in the Maigret DB? [Yn] "
                )
                .lower()
                .strip("y")
            ):
                return False

        if self.args.verbose:
            source = input("Name the source site if it is mirror: ")
            if source:
                chosen_site.source = source

        chosen_site.name = input("Change site name if you want: ") or chosen_site.name
        chosen_site.tags = list(map(str.strip, input("Site tags: ").split(',')))
        rank = Submitter.get_alexa_rank(chosen_site.url_main)
        if rank:
            print(f'New alexa rank: {rank}')
            chosen_site.alexa_rank = rank

        self.logger.debug(chosen_site.json)
        site_data = chosen_site.strip_engine_data()
        self.logger.debug(site_data.json)
        self.db.update_site(site_data)
        return True
