import re
from datetime import datetime, timedelta
from typing import Optional

import xmltodict
from requests import Session

from utils import ensure_iterable, get_http_client


def parse_timestamp(timestamp: str) -> int:
    date_format = '%Y-%m-%dT%H:%M:%SZ'
    return int(datetime.strptime(timestamp, date_format).timestamp())


def get_old_date(timestamp: str) -> str:
    date_format = '%Y-%m-%dT%H:%M:%SZ'
    created_at_minus_one = (datetime.strptime(timestamp, date_format) - timedelta(seconds=1)).strftime(date_format)

    return f'[date:"{created_at_minus_one}"]'


def get_changeset_adiff(timestamp: str) -> str:
    date_format = '%Y-%m-%dT%H:%M:%SZ'
    created_at_minus_one = (datetime.strptime(timestamp, date_format) - timedelta(seconds=1)).strftime(date_format)

    return f'[adiff:"{created_at_minus_one}","{timestamp}"]'


def get_current_adiff(timestamp: str) -> str:
    return f'[adiff:"{timestamp}"]'


def build_query_filtered(element_ids: dict, query_filter: str) -> str:
    joined_element_ids = {
        'node': ','.join(element_ids['node']) if element_ids['node'] else '-1',
        'way': ','.join(element_ids['way']) if element_ids['way'] else '-1',
        'relation': ','.join(element_ids['relation']) if element_ids['relation'] else '-1'
    }

    implicit_way_nodes = bool(query_filter)

    # default everything query filter
    if not query_filter:
        query_filter = 'node;way;relation;'

    # ensure proper query ending
    if not query_filter.endswith(';'):
        query_filter += ';'

    # expand nwr
    for match in sorted(re.finditer(r'\b(nwr)\b(?P<expand>.*?;)', query_filter),
                        key=lambda m: m.start(),
                        reverse=True):
        start, end = match.start(), match.end()
        expand = match.group('expand')

        query_filter = query_filter[:start] + f'node{expand}way{expand}rel{expand}' + query_filter[end:]

    # apply element id filtering
    for match in sorted(re.finditer(r'\b(node|way|rel(ation)?)\b', query_filter),
                        key=lambda m: m.start(),
                        reverse=True):
        end = match.end()
        element_type = match.group(1)

        # handle 'rel' alias
        if element_type == 'rel':
            element_type = 'relation'

        query_filter = query_filter[:end] + f'(id:{joined_element_ids[element_type]})' + query_filter[end:]

    if implicit_way_nodes:
        return f'({query_filter});' \
               f'out meta;' \
               f'node(w._);' \
               f'out meta;'
    else:
        return f'({query_filter});' \
               f'out meta;'


def build_query_parents_by_ids(element_ids: dict) -> str:
    return f'node(id:{",".join(element_ids["node"]) if element_ids["node"] else "-1"})->.n;' \
           f'way(id:{",".join(element_ids["way"]) if element_ids["way"] else "-1"})->.w;' \
           f'rel(id:{",".join(element_ids["relation"]) if element_ids["relation"] else "-1"})->.r;' \
           f'(way(bn.n);rel(bn.n);rel(bw.w);rel(br.r););' \
           f'out meta;'


def fetch_overpass(client: Session, post_url: str, data: str) -> dict:
    response = client.post(post_url, data={'data': data}, timeout=300)
    response.raise_for_status()
    return xmltodict.parse(response.text)


def get_current_map(actions: list) -> dict:
    result = {
        'node': {},
        'way': {},
        'relation': {}
    }

    for action in actions:
        if action['@type'] == 'create':
            element_type, element = next(iter((k, v) for k, v in action.items() if not k.startswith('@')))
        else:
            element_type, element = next(iter(action['new'].items()))
        result[element_type][element['@id']] = element

    return result


# TODO: include default actions
def parse_action(action: dict) -> (str, dict | None, dict):
    if action['@type'] == 'create':
        element_old = None
        element_type, element_new = next((k, v) for k, v in action.items() if not k.startswith('@'))
    elif action['@type'] in {'modify', 'delete'}:
        element_type, element_old = next(iter(action['old'].items()))
        element_new = next(iter(action['new'].values()))
    else:
        raise

    return element_type, element_old, element_new


def ensure_visible_tag(element: Optional[dict]) -> None:
    if not element:
        return

    if '@visible' not in element:
        element['@visible'] = 'true'


class Overpass:
    def __init__(self):
        self.base_urls = [
            'https://overpass.monicz.dev/api',
            'https://overpass-api.de/api'
        ]

    def get_changeset_elements_history(self, changeset: dict, steps: int, query_filter: str) -> Optional[dict]:
        errors = []

        for base_url in self.base_urls:
            result = self._get_changeset_elements_history(changeset, steps, query_filter, base_url)

            # everything ok
            if isinstance(result, dict):
                return result

            print(f'[2/{steps}] Retrying …')
            errors.append(result)

        # all errors are the same
        if all(errors[0] == e for e in errors[1:]):
            print(f'{errors[0]} (x{len(errors)})')
        else:
            print('❗️ Multiple errors occurred:')

            for i, error in enumerate(errors):
                print(f'[{i + 1}/{len(errors)}]: {error}')

        return None

    # noinspection PyMethodMayBeStatic
    def _get_changeset_elements_history(
            self,
            changeset: dict,
            steps: int,
            query_filter: str,
            base_url: str) -> dict | str:
        # shlink = Shlink()
        shlink_available = False  # shlink.available

        changeset_id = changeset['osm']['changeset']['@id']
        changeset_edits = []
        current_action = []

        with get_http_client() as c:
            for i, (timestamp, element_ids) in enumerate(sorted(changeset['partition'].items(), key=lambda t: t[0])):
                if query_filter:
                    old_date = get_old_date(timestamp)
                    query_unfiltered = build_query_filtered(element_ids, '')

                    old_query = f'[timeout:180]{old_date};{query_unfiltered}'
                    old_data = fetch_overpass(c, base_url + '/interpreter', old_query)

                    old = {
                        'node': {e['@id']: e for e in ensure_iterable(old_data['osm'].get('node', []))},
                        'way': {e['@id']: e for e in ensure_iterable(old_data['osm'].get('way', []))},
                        'relation': {e['@id']: e for e in ensure_iterable(old_data['osm'].get('relation', []))}
                    }
                else:
                    old = None

                partition_adiff = get_changeset_adiff(timestamp)
                current_adiff = get_current_adiff(timestamp)
                query_filtered = build_query_filtered(element_ids, query_filter)

                partition_query = f'[timeout:180]{partition_adiff};{query_filtered}'
                partition_diff = fetch_overpass(c, base_url + '/interpreter', partition_query)
                partition_action = ensure_iterable(partition_diff['osm'].get('action', []))

                if parse_timestamp(partition_diff['osm']['meta']['@osm_base']) <= parse_timestamp(timestamp):
                    return '🕒️ Overpass is updating, please try again shortly'

                if query_filter:
                    dedup_node_ids = set()

                    for action in partition_action:
                        element_type, element_old, element_new = parse_action(action)

                        # cleanup extra nodes
                        if element_type == 'node':
                            # nodes of filtered query elements are often unrelated (skeleton)
                            if element_new['@changeset'] != changeset_id:
                                continue

                            # the output may contain duplicate nodes due to double out …;
                            if element_new['@id'] in dedup_node_ids:
                                continue

                            dedup_node_ids.add(element_new['@id'])

                        # merge old data
                        if element_old:
                            if element_new['@id'] not in old[element_type]:
                                return f'❓️ Overpass data is incomplete (missing_old)'

                            element_old = old[element_type][element_new['@id']]

                        elif element_new['@version'] != '1' and element_new['@id'] in old[element_type]:
                            # TODO: ensure data integrity with OSM (visible=false; not in case)
                            element_old = old[element_type][element_new['@id']]

                        changeset_edits.append((element_type, element_old, element_new))

                else:
                    partition_size = len(partition_action)
                    query_size = sum(len(v) for v in element_ids.values())

                    if partition_size != query_size:
                        return f'❓️ Overpass data is incomplete: {partition_size} != {query_size}'

                    changeset_edits.extend(parse_action(a) for a in partition_action)

                current_query = f'[timeout:180]{current_adiff};{query_filtered}'
                current_diff = fetch_overpass(c, base_url + '/interpreter', current_query)
                current_partition_action = ensure_iterable(current_diff['osm'].get('action', []))
                current_action.extend(current_partition_action)

                # BLOCKED: https://github.com/shlinkio/shlink/issues/1674
                # if shlink_available:
                #     try:
                #         query_long_url = base_url + f'/convert?data={quote_plus(changeset_data)}&target=mapql'
                #         query_short_url = shlink.shorten(query_long_url)
                #         print(f'[{i + 2}/{steps}] Partition OK ({partition_size}); Query: {query_short_url}')
                #     except Exception:
                #         traceback.print_exc()
                #         shlink_available = False
                #         print('⚡️ Shlink is not available (query preview disabled)')

                if not shlink_available:
                    print(f'[{i + 2}/{steps}] Partition #{i + 1}: OK')

        current_map = get_current_map(current_action)

        result = {
            'node': [],
            'way': [],
            'relation': []
        }

        for element_type, element_old, element_new in changeset_edits:
            if element_new['@changeset'] != changeset_id:
                return '❓ Overpass data is corrupted (bad_changeset)'

            if element_old and int(element_new['@version']) - int(element_old['@version']) != 1:
                return '❓ Overpass data is corrupted (bad_version)'

            if not element_old and int(element_new['@version']) == 2:
                return '❓ Overpass data is corrupted (impossible_create)'

            timestamp = parse_timestamp(element_new['@timestamp'])
            element_id = element_new['@id']
            element_current = current_map[element_type].get(element_id, element_new)

            ensure_visible_tag(element_old)
            ensure_visible_tag(element_new)
            ensure_visible_tag(element_current)

            result[element_type].append((timestamp, element_id, element_old, element_new, element_current))

        return result

    def update_parents(self, invert: dict) -> int:
        base_url = self.base_urls[0]

        invert_ids = {
            'node': {e['@id'] for e in invert['node']},
            'way': {e['@id'] for e in invert['way']},
            'relation': {e['@id'] for e in invert['relation']}
        }

        deleting_ids = {
            'node': {e['@id'] for e in invert['node'] if e['@visible'] == 'false'},
            'way': {e['@id'] for e in invert['way'] if e['@visible'] == 'false'},
            'relation': {e['@id'] for e in invert['relation'] if e['@visible'] == 'false'}
        }

        if sum(len(el) for el in deleting_ids.values()) == 0:
            return 0

        query_by_ids = build_query_parents_by_ids(deleting_ids)

        with get_http_client() as c:
            parents_query = f'[timeout:180];{query_by_ids}'
            data = fetch_overpass(c, base_url + '/interpreter', parents_query)

        parents = {
            'node': ensure_iterable(data['osm'].get('node', [])),
            'way': ensure_iterable(data['osm'].get('way', [])),
            'relation': ensure_iterable(data['osm'].get('relation', [])),
        }

        fixed_parents = 0

        for element_type, elements in parents.items():
            for element in elements:
                assert isinstance(element, dict)

                if element['@id'] in invert_ids[element_type]:
                    continue

                if element_type == 'way':
                    element['nd'] = [
                        n for n in ensure_iterable(element.get('nd', []))
                        if n['@ref'] not in deleting_ids['node']
                    ]

                    # delete single node ways
                    if len(element['nd']) == 1:
                        element['nd'] = []

                    if not element['nd']:
                        element['@visible'] = 'false'
                elif element_type == 'relation':
                    element['member'] = [
                        m for m in ensure_iterable(element.get('member', []))
                        if m['@ref'] not in deleting_ids[m['@type']]
                    ]

                    if not element['member']:
                        element['@visible'] = 'false'
                else:
                    raise

                ensure_visible_tag(element)
                invert[element_type].append(element)
                fixed_parents += 1

        return fixed_parents
