#!/usr/bin/env python
# -*- coding: utf-8 -*-
# WebSearch gate for OSMNames-SphinxSearch
#
# Copyright (C) 2016 Klokan Technologies GmbH (http://www.klokantech.com/)
#   All Rights Reserved
# Unauthorized copying of this file, via any medium is strictly prohibited
# Proprietary and confidential
# Author: Martin Mikita (martin.mikita @ klokantech.com)
# Date: 15.07.2016

from flask import Flask, request, Response, render_template, url_for
from pprint import pprint, pformat, PrettyPrinter
from json import dumps
from os import getenv, path, utime
from time import time, mktime
from datetime import datetime
import requests
import sys
import MySQLdb
import re
import natsort
import rfc822 # Used for parsing RFC822 into datetime
import email # Used for formatting TS into RFC822

app = Flask(__name__, template_folder='templates/')
app.debug = not getenv('WEBSEARCH_DEBUG') is None
app.debug = True


# Return maximal number of results
SEARCH_MAX_COUNT = 100
SEARCH_DEFAULT_COUNT = 20
if getenv('SEARCH_MAX_COUNT'):
    SEARCH_MAX_COUNT = int(getenv('SEARCH_MAX_COUNT'))
if getenv('SEARCH_DEFAULT_COUNT'):
    SEARCH_DEFAULT_COUNT = int(getenv('SEARCH_DEFAULT_COUNT'))

TMPFILE_DATA_TIMESTAMP = "/tmp/osmnames-sphinxsearch-data.timestamp"

# Prepare global variable for Last-modified Header
try:
    mtime = path.getmtime(TMPFILE_DATA_TIMESTAMP)
except OSError:
    with open(TMPFILE_DATA_TIMESTAMP, 'a'):
        utime(TMPFILE_DATA_TIMESTAMP, None)
    mtime = time()
DATA_LAST_MODIFIED = email.utils.formatdate(mtime, usegmt=True)

# Filter attributes values
# dict[ attribute ] = list(values)
CHECK_ATTR_FILTER = ['country_code', 'class']
ATTR_VALUES = {}


# ---------------------------------------------------------
"""
Get attributes distinct values, using data from index
dict[ attribute ] = list(values)
"""
def get_attributes_values(index, attributes):
    global ATTR_VALUES

    # connect to the mysql server
    # default server configuration
    host = '127.0.0.1'
    port = 9306
    if getenv('WEBSEARCH_SERVER'):
        host = getenv('WEBSEARCH_SERVER')
    if getenv('WEBSEARCH_SERVER_PORT'):
        port = int(getenv('WEBSEARCH_SERVER_PORT'))

    try:
        db = MySQLdb.connect(host=host, port=port, user='root')
        cursor = db.cursor()
    except Exception as ex:
        return False

    # Loop over attributes
    if isinstance(attributes, str):
        attributes = [attributes,]

    for attr in attributes:
        # clear values
        ATTR_VALUES[attr] = []
        count = 200
        total_found = 0
        # get attributes values for index
        sqlQuery = 'SELECT {} FROM {} GROUP BY {} LIMIT {}, {}'
        sqlMeta = 'SHOW META LIKE %s'
        found = 0
        try:
            while total_found == 0 or found < total_found:
                q = cursor.execute(sqlQuery.format(attr, index, attr, found, count), ())
                for row in cursor:
                    found += 1
                    ATTR_VALUES[attr].append( str(row[0]) )
                if total_found == 0:
                    q = cursor.execute(sqlMeta, ('total_found',))
                    for row in cursor:
                        total_found = int(row[1])
                        # Skip this attribute, if total found is more than max_matches
                        if total_found > 1000:
                            del(ATTR_VALUES[attr])
                            found = total_found
            if found == 0:
                del(ATTR_VALUES[attr])
        except Exception as ex:
            print(str(ex))
            return False
    return True



# ---------------------------------------------------------
"""
Process query to Sphinx searchd with mysql
"""
def process_query_mysql(index, query, query_filter, start=0, count=0, field_weights=''):
    # default server configuration
    host = '127.0.0.1'
    port = 9306
    if getenv('WEBSEARCH_SERVER'):
        host = getenv('WEBSEARCH_SERVER')
    if getenv('WEBSEARCH_SERVER_PORT'):
        port = int(getenv('WEBSEARCH_SERVER_PORT'))

    if count == 0:
        count = SEARCH_DEFAULT_COUNT
    count = min(SEARCH_MAX_COUNT, count)

    status = True
    result = {
        'total_found': 0,
        'matches': [],
        'message': None,
        'start_index': start,
        'count': count,
        'status': status,
    }

    try:
        db = MySQLdb.connect(host=host, port=port, user='root')
        cursor = db.cursor()
    except Exception as ex:
        status = False
        result['message'] = str(ex)
        result['status'] = status
        return status, result

    argsFilter = []
    whereFilter = []

    # Prepare filter for query
    for f in ['class', 'type', 'street', 'city', 'county', 'state', 'country_code', 'country']:
        if f not in query_filter or query_filter[f] is None:
            continue
        inList = []
        for val in query_filter[f]:
            if f in ATTR_VALUES and val not in ATTR_VALUES[f]:
                status = False
                result['message'] = 'Invalid attribute value.'
                result['status'] = status
                return status, result
            argsFilter.append(val)
            inList.append('%s')
        # Creates where condition: f in (%s, %s, %s...)
        whereFilter.append('{} in ({})'.format(f, ', '.join(inList)))

    # Prepare viewbox filter
    if 'viewbox' in query_filter and query_filter['viewbox'] is not None:
        bbox = query_filter['viewbox'].split(',')
        # latitude, south, north
        whereFilter.append('({:.12f} < lat AND lat < {:.12f})'
            .format(float(bbox[0]), float(bbox[2])))
        # longtitude, west, east
        whereFilter.append('({:.12f} < lon AND lon < {:.12f})'
            .format(float(bbox[1]), float(bbox[3])))

    # MATCH query should be last in the WHERE condition
    # Prepare query
    whereFilter.append('MATCH(%s)')
    argsFilter.append(query)

    sortBy = []
    # Prepare sorting by custom or default
    if 'sortBy' in query_filter and query_filter['sortBy'] is not None:
        for attr in query_filter['sortBy']:
            attr = attr.split('-')
            # List of supported sortBy columns - to prevent SQL injection
            if attr[0] not in ('class', 'type', 'street', 'city',
                'county', 'state', 'country_code', 'country',
                'importance' 'weight', 'id'):
                print >> sys.stderr, 'Invalid sortBy column ' + attr[0]
                continue
            asc = 'ASC'
            if len(attr) > 1 and (attr[1] == 'desc' or attr[1] == 'DESC'):
                asc = 'DESC'
            sortBy.append('{} {}'.format(attr[0], asc))

    if len(sortBy) == 0:
        sortBy.append('weight DESC')


    # Field weights and other options
    # ranker=expr('sum(lcs*user_weight)*1000+bm25') == SPH_RANK_PROXIMITY_BM25
    # ranker=expr('sum((4*lcs+2*(min_hit_pos==1)+exact_hit)*user_weight)*1000+bm25') == SPH_RANK_SPH04
    # ranker=expr('sum((4*lcs+2*(min_hit_pos==1)+100*exact_hit)*user_weight)*1000+bm25') == SPH_RANK_SPH04 boosted with exact_hit
    # select @weight+IF(fieldcrc==$querycrc,10000,0) AS weight
    # options:
    #  - 'cutoff' - integer (max found matches threshold)
    #  - 'max_matches' - integer (per-query max matches value), default 1000
    #  - 'max_query_time' - integer (max search time threshold, msec)
    #  - 'retry_count' - integer (distributed retries count)
    #  - 'retry_delay' - integer (distributed retry delay, msec)
    option = "retry_count = 2, retry_delay = 500, max_matches = 200, max_query_time = 20000"
    option += ", cutoff = 2000"
    option += ", ranker=expr('sum((10*lcs+5*exact_order+10*exact_hit+5*wlccs)*user_weight)*1000+bm25')"
    if len(field_weights) > 0:
        option += ", field_weights = ({})".format(field_weights)
    # Prepare query for boost
    query_elements = re.compile("\s*,\s*|\s+").split(query)
    select_boost = []
    argsBoost = []
    # Boost whole query (street with spaces)
    # select_boost.append('IF(name=%s,1000000,0)')
    # argsBoost.append(re.sub(r"\**", "", query))
    # Boost each query part delimited by space
    # Only if there is more than 1 query elements
    if False and len(query_elements) > 1:
        for qe in query_elements:
           select_boost.append('IF(name=%s,1000000,0)')
           argsBoost.append(re.sub(r"\**", "", qe))

    # Prepare SELECT
    sql = "SELECT WEIGHT()*importance+{} as weight, * FROM {} WHERE {} ORDER BY {} LIMIT %s, %s OPTION {};".format(
        '+'.join(select_boost) if len(select_boost) > 0 else '0',
        index,
        ' AND '.join(whereFilter),
        ', '.join(sortBy),
        option
    )

    try:
        args = argsBoost + argsFilter + [start, count]
        q = cursor.execute(sql, args)
        # pprint([sql, args, cursor._last_executed, q])
        desc = cursor.description
        matches = []
        for row in cursor:
            match = {
                'weight' : 0,
                'attrs' : {},
                'id' : 0,
            }
            for (name, value) in zip(desc, row):
                col = name[0]
                if col == 'id':
                    match['id'] = value
                elif col == 'weight':
                    match['weight'] = value
                else:
                    match['attrs'][col] = value
            matches.append(match)
        # ~ for row in cursor
        result['matches'] = matches

        q = cursor.execute('SHOW META LIKE %s', ('total_found',))
        for row in cursor:
            result['total_found'] = int(row[1])
    except Exception as ex:
        status = False
        result['message'] = str(ex)

    result['status'] = status
    return status, result



# ---------------------------------------------------------
"""
Merge two result objects into one
Order matches by weight
"""
def mergeResultObject(result_old, result_new):
    # Merge matches
    weight_matches = {}
    unique_id = 0
    unique_ids_list = []

    for matches in [result_old['matches'], result_new['matches'], ]:
        for row in matches:
            if row['id'] in unique_ids_list:
                result_old['total_found'] -= 1 # Decrease total found number
                continue
            unique_ids_list.append(row['id'])
            weight = str(row['weight'])
            if weight in weight_matches:
                weight += '_{}'.format(unique_id)
                unique_id += 1
            weight_matches[weight] = row

    # Sort matches according to the weight and unique id
    sorted_matches = natsort.natsorted(weight_matches.items(), reverse=True)
    matches = []
    i = 0
    for row in sorted_matches:
        matches.append(row[1])
        i += 1
        # Append only first #count rows
        if i >= result_old['count']:
            break

    result = result_old.copy()
    result['matches'] = matches
    result['total_found'] += result_new['total_found']
    if 'message' in result_new and result_new['message']:
        result['message'] = ', '.join(result['message'], result_new['message'])

    return result



# ---------------------------------------------------------
"""
Prepare JSON from pure Result array from SphinxQL
"""
def prepareResultJson(result, query_filter):

    if 'start_index' not in result:
        result = {
            'start_index': 0,
            'count': 0,
            'total_found': 0,
            'matches': [],
        }

    response = {
        'results': [],
        'startIndex': result['start_index'],
        'count': result['count'],
        'totalResults': result['total_found'],
    }
    if 'message' in result and result['message']:
        response['message'] = result['message']

    for row in result['matches']:
        r = row['attrs']
        res = {'rank': row['weight'], 'id': row['id']}
        for attr in r:
            if isinstance(r[attr], str):
                res[attr] = r[attr].decode('utf-8')
            else:
                res[ attr ] = r[attr]
        # res['boundingbox'] = "{}, {}, {}, {}".format(r['north'], r['south'], r['east'], r['west'])
        res['boundingbox'] = [res['west'], res['south'], res['east'], res['north']]
        del res['west']
        del res['south']
        del res['east']
        del res['north']
        # Empty values for KlokanTech NominatimMatcher JS
        # res['address'] = {
        #     'country_code': '',
        #     'country': '',
        #     'city': None,
        #     'town': None,
        #     'village': None,
        #     'hamlet': rr['name'],
        #     'suburb': '',
        #     'pedestrian': '',
        #     'house_number': '1'
        # }
        response['results'].append(res)

    # Prepare next and previous index
    nextIndex = result['start_index'] + result['count']
    if nextIndex <= result['total_found']:
        response['nextIndex'] = nextIndex
    prevIndex = result['start_index'] - result['count']
    if prevIndex >= 0:
        response['previousIndex'] = prevIndex

    return response



# ---------------------------------------------------------

"""
Parse and prepare name_suffix based on results
"""
def prepareNameSuffix(results):

    counts = {'country_code': [], 'state': [], 'city': []}

    # Separate different country codes
    for row in results:
        for field in ['country_code', 'state', 'city']:
            if row[field] in counts[field]:
                continue
            # Skip states for not-US
            if row['country_code'] != 'us' and field == 'state':
                continue
            counts[field].append(row[field])

    # Prepare name suffix based on counts
    newresults = []
    for row in results:
        name_suffix = []
        if row['type'] != 'city' and len(counts['city']) > 1 and len(row['city']) > 0:
            name_suffix.append(row['city'])
        if row['country_code'] == 'us' and len(counts['state']) > 1 and len(row['state']) > 0:
            name_suffix.append(row['state'])
        if len(counts['country_code']) > 1:
            name_suffix.append(row['country_code'].upper())
        row['name_suffix'] = ', '.join(name_suffix)
        newresults.append(row)

    return newresults


"""
Format response output
"""
def formatResponse(data, code=200):
    # Format json - return empty
    result = data['result'] if 'result' in data else {}
    format = 'json'
    if request.args.get('format'):
        format = request.args.get('format')
    if 'format' in data:
        format = data['format']

    tpl = data['template'] if 'template' in data else 'answer.html'
    if format == 'html' and tpl is not None:
        if not 'route' in data:
            data['route'] = '/'
        return render_template(tpl, rc=(code == 200), **data), code

    json = dumps( result )
    mime = 'application/json'
    # Append callback for JavaScript
    if request.args.get('json_callback'):
        json = request.args.get('json_callback') + "("+json+");";
        mime = 'application/javascript'
    if request.args.get('callback'):
        json = request.args.get('callback') + "("+json+");";
        mime = 'application/javascript'
    resp = Response(json, mimetype=mime)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    # Cache results for 4 hours in Web Browsers and 12 hours in CDN caches
    resp.headers['Cache-Control'] = 'public, max-age=14400, s-maxage=43200'
    resp.headers['Last-Modified'] = DATA_LAST_MODIFIED
    return resp, code


# ---------------------------------------------------------
"""
Modify query - add asterisk for each element of query, set original query
"""
def modify_query_autocomplete(orig_query):
    query = '* '.join(re.compile("\s*,\s*|\s+").split(orig_query)) + '*'
    query = re.sub(r"\*+", "*", query)
    return query, orig_query

"""
Modify query - use and set original
"""
def modify_query_orig(orig_query):
    return orig_query, orig_query

"""
Modify query - remove house number, use and set modified query
"""
def modify_query_remhouse(orig_query):
    # Remove any number from the request
    query = re.sub(r"\d+([/, ]\d+)?", "", orig_query)
    if query == orig_query:
        return None, orig_query
    return query, query

"""
Modify query - split query elements as OR, use modified and set original query
"""
def modify_query_splitor(orig_query):
    if orig_query.startswith('@'):
        return None, orig_query
    query = ' | '.join(re.compile("\s*,\s*|\s+").split(orig_query))
    if query == orig_query:
        return None, orig_query
    return query, orig_query



# ---------------------------------------------------------
"""
Process array of modifiers and return results
"""
def process_query_modifiers(orig_query, index_modifiers, debug_result, times,
    query_filter, start, count, debug = False):
    rc = False
    result = {}
    proc_query = orig_query
    # Pair is (index, modify_function, [field_weights, [orig_query]])
    for pair in index_modifiers:
        index = pair[0]
        modify = pair[1]
        field_weights = ''
        if len(pair) >= 3:
            field_weights = pair[2]
        if len(pair) >= 4:
            proc_query = pair[3]
        if debug and index not in times:
            times[index] = {}
        # Cycle through few modifications of query
        # Modification function return query with original query (possibly modified) used for the following processing
        query, proc_query = modify(proc_query)
        # No modification has been done
        if query is None:
            continue
        # Process modified query
        if debug:
            times['start_query'] = time()
        rc, result_new = process_query_mysql(index, query, query_filter,
                start, count, field_weights)
        if debug:
            times[index][modify.__name__] = time() - times['start_query']
        if rc and 'matches' in result_new and len(result_new['matches']) > 0:
            # Merge matches with previous result
            if 'matches' in result and len(result['matches']) > 0:
                result = mergeResultObject(result, result_new)
            else:
                result = result_new.copy()
                debug_result['modify'] = []
                debug_result['query_succeed'] = []
                debug_result['index_succeed'] = []
            debug_result['modify'].append(modify.__name__)
            debug_result['query_succeed'].append(query.decode('utf-8'))
            debug_result['index_succeed'].append(index.decode('utf-8'))
            # Only break, if we have enough matches
            if len(result['matches']) >= result['count']:
                break
        else:
            result = result_new
    # for pair in index_modifiers
    return rc, result



# ---------------------------------------------------------
"""
Common search method
"""
def search(orig_query, query_filter, autocomplete=False, start=0, count=0,
        debug=False, times={}, debug_result={}):

    # Iterating only over 3 index
    # 1. Boosted prefix+exact on name
    # 2. Prefix on names - full text
    # 3. Infix with Soundex on names - full text
    index_modifiers = []

    # Pair is (index, modify_function, [field_weights, [index_weights, [orig_query]]])

    # 1. Boosted name
    if autocomplete:
        index_modifiers.append( ('ind_name_exact',
                modify_query_autocomplete,
                'name = 1000, alternative_names = 990',
            ) )
        index_modifiers.append( ('ind_name_prefix',
                modify_query_autocomplete,
                'name = 900, alternative_names = 890',
            ) )
    index_modifiers.append( ('ind_name_exact',
            modify_query_orig,
            'name = 800, alternative_names = 790',
        ) )
    index_modifiers.append( ('ind_name_prefix',
            modify_query_orig,
            'name = 700, alternative_names = 690',
        ) )
    index_modifiers.append( ('ind_name_exact',
            modify_query_remhouse,
            'name = 600, alternative_names = 590',
            orig_query,
        ) )
    index_modifiers.append( ('ind_name_prefix',
            modify_query_remhouse,
            'name = 500, alternative_names = 490',
            orig_query,
        ) )

    # 2. Prefix on names
    if autocomplete:
        index_modifiers.append( ('ind_names_prefix',
                modify_query_autocomplete,
                'name = 300, alternative_names = 290, display_name = 70',
            ) )
    index_modifiers.append( ('ind_names_prefix',
            modify_query_orig,
            'name = 200, alternative_names = 190, display_name = 60',
        ) )
    index_modifiers.append( ('ind_names_prefix',
            modify_query_remhouse,
            'name = 100, alternative_names = 95, display_name = 50',
            orig_query,
        ) )

    if debug:
        pprint(index_modifiers)

    result = {'matches':[]}
    # 1. + 2.
    rc, result = process_query_modifiers(orig_query, index_modifiers, debug_result,
        times, query_filter, start, count, debug)

    if debug:
        pprint(rc)
        pprint(result)

    result_first = result
    # 3. + 4.
    if not rc or not 'matches' in result or len(result['matches']) == 0:
        index_modifiers = []
        # 3. Infix with soundex on names
        if autocomplete:
            index_modifiers.append( ('ind_names_infix_soundex',
                    modify_query_autocomplete,
                    'name = 90, alternative_names = 89, display_name = 40',
                ) )
        index_modifiers.append( ('ind_names_infix_soundex',
                modify_query_orig,
                'name = 70, alternative_names = 69, display_name = 20',
            ) )
        index_modifiers.append( ('ind_names_infix_soundex',
                modify_query_remhouse,
                'name = 50, alternative_names = 49, display_name = 10',
                orig_query,
            ) )
        # 4. If no result were found, try splitor modifier on prefix and infix soundex
        index_modifiers.append( ('ind_names_prefix',
                modify_query_splitor,
                'name = 20, alternative_names = 19, display_name = 1',
            ) )
        index_modifiers.append( ('ind_names_infix_soundex',
                modify_query_splitor,
                'name = 10, alternative_names = 9, display_name = 1',
            ) )
        rc, result = process_query_modifiers(orig_query, index_modifiers, debug_result,
            times, query_filter, start, count, debug)

    if debug:
        pprint(rc)
        pprint(result)

    if 'matches' not in result:
        result = result_first

    return rc, result



# ---------------------------------------------------------
"""
Check request header for 'if-modified-since'
Return True if content wasn't modified (According to the timestamp)
"""
def has_modified_header(headers):
    global DATA_LAST_MODIFIED

    modified = headers.get('if-modified-since')
    if modified:
        oldLastModified = DATA_LAST_MODIFIED
        try:
            mtime = path.getmtime(TMPFILE_DATA_TIMESTAMP)
        except OSError:
            with open(TMPFILE_DATA_TIMESTAMP, 'a'):
                utime(TMPFILE_DATA_TIMESTAMP, None)
            mtime = time()
        DATA_LAST_MODIFIED = email.utils.formatdate(mtime, usegmt=True)
        if DATA_LAST_MODIFIED != oldLastModified:
            # reload attributes if index changed
            get_attributes_values('ind_name_exact', CHECK_ATTR_FILTER)
        # pprint([headers, modified, DATA_LAST_MODIFIED, mtime])
        # pprint([mtime, rfc822.parsedate(modified), mktime(rfc822.parsedate(modified))])
        modified_file = datetime.fromtimestamp(mtime)
        modified_file = modified_file.replace(microsecond = 0)
        modified_date = datetime.fromtimestamp(mktime(rfc822.parsedate(modified)))

        # pprint([
        #     'Data: ', modified_file,
        #     'Header: ', modified_date,
        #     modified_file <= modified_date,
        # ])
        if modified_file <= modified_date:
            return True

    return False



# ---------------------------------------------------------
"""
Autocomplete searching via HTTP URL
"""
@app.route('/q/<query>', defaults={'country_code': None})
@app.route('/<country_code>/q/<query>')
def search_url(country_code, query):
    autocomplete = True
    code = 400
    data = {'query': '', 'route': '/', 'format': 'json'}
    query_filter = {}

    if has_modified_header(request.headers):
        data['result'] = {}
        return formatResponse(data, 304)

    if country_code is not None:
        if len(country_code) > 3:
            data['result'] = {'message': 'Invalid country code value.'}
            return formatResponse(data, code)
        query_filter = {'country_code': country_code.encode('utf-8').split(',')}

    # Common search for query with filters
    rc, result = search(query.encode('utf-8'), query_filter, autocomplete)
    if rc and len(result['matches']) > 0:
        code = 200

    data['query'] = query
    data['result'] = prepareResultJson(result, query_filter)

    return formatResponse(data, code)


# Alias without redirect
@app.route('/q/<query>.js', defaults={'country_code': None})
@app.route('/<country_code>/q/<query>.js')
def search_url_js(country_code, query):
    return search_url(country_code, query)



# ---------------------------------------------------------
"""
Global searching via HTTP Query
"""
@app.route('/')
def search_query():
    data = {'query': '', 'route': '/', 'template': 'answer.html'}
    layout = request.args.get('layout')
    if layout and layout in ('answer', 'home'):
        data['template'] = request.args.get('layout') + '.html'
    code = 400

    if has_modified_header(request.headers):
        data['result'] = {}
        return formatResponse(data, 304)

    q = request.args.get('q')
    autocomplete = request.args.get('autocomplete')
    debug = request.args.get('debug')
    if debug:
        pprint([q, autocomplete, debug])

    times = {}
    debug_result = {}
    if debug:
        times['start'] = time()

    query_filter = {
        'type': None, 'class': None,
        'street': None, 'city' : None,
        'county': None, 'state': None,
        'country': None, 'country_code': None,
        'viewbox': None,
        'sortBy': None,
    }
    filter = False
    for f in query_filter:
        if request.args.get(f):
            v = None
            # Some arguments may be list
            if f in ('type', 'class', 'city', 'county', 'country_code', 'sortBy', 'tags'):
                vl = request.args.getlist(f)
                if len(vl) == 1:
                    v = vl[0].encode('utf-8')
                    # This argument can be list separated by comma
                    v = v.split(',')
                elif len(vl) > 1:
                    v = [x.encode('utf-8') for x in vl]
            if v is None:
                vl = request.args.get(f)
                v = vl.encode('utf-8')
            query_filter[f] = v
            filter = True

    if not q and not filter:
        # data['result'] = {'error': 'Missing query!'}
        return render_template('home.html', route='/')

    data['url'] = request.url
    data['query'] = q.encode('utf-8')
    orig_query = data['query']

    start = 0
    count = 0
    if request.args.get('startIndex'):
        try:
            start = int(request.args.get('startIndex'))
        except:
            pass
    if request.args.get('count'):
        try:
            count = int(request.args.get('count'))
        except:
            pass

    if debug:
        times['prepare'] = time() - times['start']

    # Common search for query with filters
    rc, result = search(orig_query, query_filter, autocomplete, start, count, debug, times, debug_result)
    if rc and len(result['matches']) > 0:
        code = 200

    data['query'] = orig_query.decode('utf-8')
    if debug:
        times['process'] = time() - times['start']
        debug_result['times'] = times
    data['result'] = prepareResultJson(result, query_filter)
    if len(data['result']['results']) > 0 :
        data['result']['results'] = prepareNameSuffix(data['result']['results'])
    data['debug_result'] = debug_result
    data['autocomplete'] = autocomplete
    data['debug'] = debug
    args = dict(request.args)
    if 'layout' in args:
        del(args['layout'])
    data['url_home'] = url_for('search_query', layout='home', **args)
    return formatResponse(data, code)



# ---------------------------------------------------------

class MyPrettyPrinter(PrettyPrinter):
    def format(self, object, context, maxlevels, level):
        if isinstance(object, unicode):
            return ('"'+object.encode('utf-8')+'"', True, False)
        return PrettyPrinter.format(self, object, context, maxlevels, level)

"""
Custom template filter - nl2br
"""
@app.template_filter()
def nl2br(value):
    if isinstance(value, dict):
        for key in value:
            value[key] = nl2br(value[key])
        return value
    elif isinstance(value, str):
        return value.replace('\n', '<br>')
    else:
        return value


"""
Custom template filter - ppretty
"""
@app.template_filter()
def ppretty(value):
    return MyPrettyPrinter().pformat(value).decode('utf-8')



# =============================================================================

# Load attributes at runtime
get_attributes_values('ind_name_exact', CHECK_ATTR_FILTER)
pprint(ATTR_VALUES)

"""
Main launcher
"""
if __name__ == '__main__':
        app.run(threaded=False, host='0.0.0.0', port=8000)

