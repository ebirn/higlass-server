# -*- coding: utf-8 -*-
from __future__ import print_function

import base64
import csv
import clodius.hdf_tiles as hdft
import clodius.db_tiles as cdt
import collections as col
import django.db.models as dbm
import django.db.models.functions as dbmf
import cooler.contrib.higlass as cch
import guardian.utils as gu
import higlass_server.settings as hss
import itertools as it
import h5py
import json
import logging
import math
import numpy as np
import os.path as op
import rest_framework.exceptions as rfe
import rest_framework.pagination as rfpa
import rest_framework.parsers as rfp
import rest_framework.status as rfs
import tilesets.models as tm
import tilesets.permissions as tsp
import tilesets.serializers as tss
import tilesets.suggestions as tsu
import slugid
import urllib

try:
    import cPickle as pickle
except ImportError:
    import pickle

from django.core.exceptions import ObjectDoesNotExist
from django.contrib.auth.models import User
from django.http import JsonResponse, HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.gzip import gzip_page
from rest_framework import generics
from rest_framework import viewsets
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.authentication import BasicAuthentication
from fragments.drf_disable_csrf import CsrfExemptSessionAuthentication
from .tiles import make_tiles

from higlass_server.utils import getRdb

logger = logging.getLogger(__name__)

global mats
mats = {}

rdb = getRdb()


def make_mats(dset):
    f = h5py.File(dset, 'r')
    info = cch.get_info(dset)

    info["min_pos"] = [int(m) for m in info["min_pos"]]
    info["max_pos"] = [int(m) for m in info["max_pos"]]
    info["max_zoom"] = int(info["max_zoom"])
    info["max_width"] = int(info["max_width"])

    if "transforms" in info:
        info["transforms"] = list(info["transforms"])

    mats[dset] = [f, info]


def format_cooler_tile(tile_data_array):
    '''
    Format raw cooler cooler data into a more structured tile
    containing either float16 or float32 data along with a 
    dtype to differentiate between the two.

    Parameters
    ----------
    tile_data_array: np.array
        An array containing a flattened 256x256 chunk of data

    Returns
    -------
    tile_data: {'dense': str, 'dtype': str}
        The tile data reformatted to use float16 or float32 as the
        datatype. The dtype indicates which format is chosen.
    '''

    tile_data = {}

    min_dense = float(np.min(tile_data_array))
    max_dense = float(np.max(tile_data_array))

    tile_data["min_value"] = min_dense
    tile_data["max_value"] = max_dense

    min_f16 = np.finfo('float16').min
    max_f16 = np.finfo('float16').max

    if (
        max_dense > min_f16 and max_dense < max_f16 and
        min_dense > min_f16 and min_dense < max_f16
    ):
        tile_data['dense'] = base64.b64encode(tile_data_array.astype('float16')).decode('latin-1')
        tile_data['dtype'] = 'float16'
    else:
        tile_data['dense'] = base64.b64encode(tile_data_array.astype('float32')).decode('latin-1')
        tile_data['dtype'] = 'float32'

    return tile_data


def extract_tileset_uid(tile_id):
    '''
    Get the tileset uid from a tile id. Should usually be all the text
    before the first dot.

    Parameters
    ----------
    tile_id : str
        The id of the tile we're getting the tileset info for (e.g. xyz.0.0.1)
    Returns
    -------
    tileset_uid : str
        The uid of the tileset that this tile comes from
    '''
    tile_id_parts = tile_id.split('.')
    tileset_uuid = tile_id_parts[0]

    return tileset_uuid

def generate_hitile_tiles(tileset, tile_ids):
    '''
    Generate tiles from a hitile file.

    Parameters
    ----------
    tileset: tilesets.models.Tileset object
        The tileset that the tile ids should be retrieved from
    tile_ids: [str,...]
        A list of tile_ids (e.g. xyx.0.0) identifying the tiles
        to be retrieved

    Returns
    -------
    tile_list: [(tile_id, tile_data),...]
        A list of tile_id, tile_data tuples
    '''
    generated_tiles = []

    for tile_id in tile_ids:
        tile_id_parts = tile_id.split('.')
        tile_position = list(map(int, tile_id_parts[1:3]))

        dense = hdft.get_data(
            h5py.File(
                get_datapath(tileset.datafile.url)
            ),
            tile_position[0],
            tile_position[1]
        )

        if len(dense):
            max_dense = max(dense)
            min_dense = min(dense)
        else:
            max_dense = 0
            min_dense = 0

        min_f16 = np.finfo('float16').min
        max_f16 = np.finfo('float16').max

        has_nan = len([d for d in dense if np.isnan(d)]) > 0

        if (
            not has_nan and
            max_dense > min_f16 and max_dense < max_f16 and
            min_dense > min_f16 and min_dense < max_f16
        ):
            tile_value = {
                'dense': base64.b64encode(dense.astype('float16')).decode('utf-8'),
                'dtype': 'float16'
            }
        else:
            tile_value = {
                'dense': base64.b64encode(dense.astype('float32')).decode('utf-8'),
                'dtype': 'float32'
            }

        generated_tiles += [(tile_id, tile_value)]

    return generated_tiles

def generate_beddb_tiles(tileset, tile_ids):
    '''
    Generate tiles from a beddb file.

    Parameters
    ----------
    tileset: tilesets.models.Tileset object
        The tileset that the tile ids should be retrieved from
    tile_ids: [str,...]
        A list of tile_ids (e.g. xyx.0.1) identifying the tiles
        to be retrieved

    Returns
    -------
    generated_tiles: [(tile_id, tile_data),...]
        A list of tile_id, tile_data tuples
    '''
    generated_tiles = []

    for tile_id in tile_ids:
        tile_id_parts = tile_id.split('.')
        tile_position = list(map(int, tile_id_parts[1:3]))
        tile_value = cdt.get_tile(
            get_datapath(tileset.datafile.url),
            tile_position[0],
            tile_position[1]
        )

        generated_tiles += [(tile_id, tile_value)]

    return generated_tiles

def generate_bed2ddb_tiles(tileset, tile_ids):
    '''
    Generate tiles from a bed2db file.

    Parameters
    ----------
    tileset: tilesets.models.Tileset object
        The tileset that the tile ids should be retrieved from
    tile_ids: [str,...]
        A list of tile_ids (e.g. xyx.0.0.1) identifying the tiles
        to be retrieved

    Returns
    -------
    generated_tiles: [(tile_id, tile_data),...]
        A list of tile_id, tile_data tuples
    '''
    generated_tiles = []

    for tile_id in tile_ids:
        tile_id_parts = tile_id.split('.')
        tile_position = list(map(int, tile_id_parts[1:4]))
        tile_value = cdt.get_2d_tile(
            get_datapath(tileset.datafile.url),
            tile_position[0],
            tile_position[1],
            tile_position[2]
        )

        generated_tiles += [(tile_id, tile_value)]

    return generated_tiles

def generate_hibed_tiles(tileset, tile_ids):
    '''
    Generate tiles from a hibed file.

    Parameters
    ----------
    tileset: tilesets.models.Tileset object
        The tileset that the tile ids should be retrieved from
    tile_ids: [str,...]
        A list of tile_ids (e.g. xyx.0.0.1) identifying the tiles
        to be retrieved

    Returns
    -------
    generated_tiles: [(tile_id, tile_data),...]
        A list of tile_id, tile_data tuples
    '''
    generated_tiles = []
    for tile_id in tile_ids:
        tile_id_parts = tile_id.split('.')
        tile_position = list(map(int, tile_id_parts[1:3]))
        dense = hdft.get_discrete_data(
            h5py.File(
                get_datapath(tileset.datafile.url)
            ),
            tile_position[0],
            tile_position[1]
        )

        tile_value = {'discrete': list([list([x.decode('utf-8') for x in d]) for d in dense])}

        generated_tiles += [(tile_id, tile_value)]

    return generated_tiles

def get_transform_type(tile_id):
    '''
    Get the transform type specified in the tile id.

    Parameters
    ----------
    cooler_tile_id: str
        A tile id for a 2D tile (cooler)

    Returns
    -------
    transform_type: str
        The transform type requested for this tile
    '''
    tile_id_parts = tile_id.split('.')

    if len(tile_id_parts) > 4:
        transform_method = tile_id_parts[4]
    else:
        transform_method = 'default'

    return transform_method

def bin_tiles_by_zoom_level_and_transform(tile_ids):
    '''
    Place these tiles into separate lists according to their
    zoom level and transform type

    Parameters
    ----------
    tile_ids: [str,...]
        A list of tile_ids (e.g. xyx.0.0.1) identifying the tiles
        to be retrieved

    Returns
    -------
    tile_lists: [tile_ids, tile_ids]
        A list of lists of tiles each of which have the same zoom level
        and transform type
    '''
    tile_id_lists = col.defaultdict(set)

    for tile_id in tile_ids:
        tile_id_parts = tile_id.split('.')
        tile_position = list(map(int, tile_id_parts[1:4]))
        zoom_level = tile_position[0]

        transform_method = get_transform_type(tile_id)


        tile_id_lists[(zoom_level, transform_method)].add(tile_id)

    return tile_id_lists

def partition_by_adjacent_tiles(tile_ids):
    '''
    Partition a set of tile ids into sets of adjacent tiles

    Parameters
    ----------
    tile_ids: [str,...]
        A list of tile_ids (e.g. xyx.0.0.1) identifying the tiles
        to be retrieved

    Returns
    -------
    tile_lists: [tile_ids, tile_ids]
        A list of tile lists, all of which have tiles that
        are within 1 position of another tile in the list
    '''
    tile_id_lists = []

    for tile_id in sorted(tile_ids, key=lambda x: [int(p) for p in x.split('.')[2:4]]):
        tile_id_parts = tile_id.split('.')

        # exclude the zoom level in the position
        # because the tiles should already have been partitioned
        # by zoom level
        tile_position = list(map(int, tile_id_parts[2:4]))

        added = False

        for tile_id_list in tile_id_lists:
            # iterate over each group of adjacent tiles
            far_apart = False

            for ct_tile_id in tile_id_list:
                ct_tile_id_parts = ct_tile_id.split('.')
                ct_tile_position = list(map(int, ct_tile_id_parts[2:4]))

                for p1,p2 in zip(tile_position, ct_tile_position):
                    if abs(int(p1) - int(p2)) > 1:
                        # too far apart can't be part of the same group
                        far_apart = True
                        print("fa:", tile_position, ct_tile_position)

                if not far_apart:
                    # no tile found that was too far in this group
                    tile_id_list += [tile_id]
                    added = True
                    break
                
            if added:
                break
        if not added:
            tile_id_lists += [[tile_id]]

    return tile_id_lists

def generate_cooler_tiles(tileset, tile_ids):
    '''
    Generate tiles from a cooler file.

    Parameters
    ----------
    tileset: tilesets.models.Tileset object
        The tileset that the tile ids should be retrieved from
    tile_ids: [str,...]
        A list of tile_ids (e.g. xyx.0.0.1) identifying the tiles
        to be retrieved

    Returns
    -------
    generated_tiles: [(tile_id, tile_data),...]
        A list of tile_id, tile_data tuples
    '''
    filename = get_datapath(tileset.datafile.url)

    if filename not in mats:
        # check if this tileset is open
        make_mats(filename)

    tileset_file_and_info = mats[filename]

    tile_ids_by_zoom_and_transform = bin_tiles_by_zoom_level_and_transform(tile_ids).values()
    partitioned_tile_ids = list(it.chain(*[partition_by_adjacent_tiles(t) 
        for t in tile_ids_by_zoom_and_transform]))

    generated_tiles = []

    for tile_group in partitioned_tile_ids:
        zoom_level = int(tile_group[0].split('.')[1])
        tileset_id = tile_group[0].split('.')[0]
        transform_type = get_transform_type(tile_group[0])

        if zoom_level > tileset_file_and_info[1]['max_zoom']:
            # this tile has too high of a zoom level specified
            continue

        tile_positions = [[int(x) for x in t.split('.')[2:4]] for t in tile_group]

        # filter for tiles that are in bounds for this zoom level
        tile_positions = list(filter(lambda x: x[0] < 2 ** zoom_level, tile_positions))
        tile_positions = list(filter(lambda x: x[1] < 2 ** zoom_level, tile_positions))

        if len(tile_positions) == 0:
            # no in bounds tiles
            continue

        minx = min([t[0] for t in tile_positions])
        maxx = max([t[0] for t in tile_positions])

        miny = min([t[1] for t in tile_positions])
        maxy = max([t[1] for t in tile_positions])

        tile_data_by_position = make_tiles(zoom_level, minx, miny, 
                tileset_file_and_info, transform_type,
                maxx-minx+1, maxy-miny+1)

        tiles = [(".".join(map(str, [tileset_id] + [zoom_level] + list(position) + [transform_type])), format_cooler_tile(tile_data))
                for (position, tile_data) in tile_data_by_position.items()]


        generated_tiles += tiles

    return generated_tiles

def generate_tiles(tileset, tile_ids):
    '''
    Generate a tiles for the give tile_ids.

    All of the tile_ids must come from the same tileset. This function
    will determine the appropriate handler this tile given the tileset's
    filetype and datatype

    Parameters
    ----------
    tileset: tilesets.models.Tileset object
        The tileset that the tile ids should be retrieved from
    tile_ids: [str,...]
        A list of tile_ids (e.g. xyx.0.0.1) identifying the tiles
        to be retrieved

    Returns
    -------
    tile_list: [(tile_id, tile_data),...]
        A list of tile_id, tile_data tuples
    '''
    print("tileset filetype", tileset.filetype)

    if tileset.filetype == 'hitile':
        return generate_hitile_tiles(tileset, tile_ids)
    elif tileset.filetype == 'beddb':
        return generate_beddb_tiles(tileset, tile_ids)
    elif tileset.filetype == 'bed2ddb':
        return generate_bed2ddb_tiles(tileset, tile_ids)
    elif tileset.filetype == 'hibed':
        return generate_hibed_tiles(tileset, tile_ids)
    elif tileset.filetype == 'cooler':
        return generate_cooler_tiles(tileset, tile_ids)
    else:
        return [(ti, {'error': 'Unknown tileset filetype: {}'.format(tileset.filetype)}) for ti in tile_ids]

def generate_tile(tile_id, request):
    '''
    Create a tile. The tile_id specifies the dataset as well
    as the position.

    This function will look at the filetype and determine what type
    of tile to retrieve (e..g cooler -> 2D dense, hitile -> 1D dense,
    elasticsearch -> anything)

    Args:
        tile_id (str): The id of a tile, consisting of the tileset id,
            followed by the tile position (e.g. PIYqJpdyTCmAZGmA6jNHJw.4.0.0)
        request (django.http.HTTPRequest): The request that included this tile.

    Returns:
        (string, dict): A tuple containing the tile ID tile data
    '''

    tileset_uuid = extract_tileset_uid(tile_id)
    tile_id_parts = tile_id.split('.')

    tileset = tm.Tileset.objects.get(uuid=tileset_uuid)

    if tileset.private and request.user != tileset.owner:
        # dataset is not public return an empty set
        return (tileset_uuid, {'error': "Forbidden"})

    tile_value = rdb.get(tile_id)

    if tile_value is not None:
        tile_value = pickle.loads(tile_value)
        return (tile_id, tile_value)

    if tileset.filetype == "hitile":
        tile_position = list(map(int, tile_id_parts[1:3]))

        dense = hdft.get_data(
            h5py.File(
                get_datapath(tileset.datafile.url)
            ),
            tile_position[0],
            tile_position[1]
        )

        if len(dense):
            max_dense = max(dense)
            min_dense = min(dense)
        else:
            max_dense = 0
            min_dense = 0

        min_f16 = np.finfo('float16').min
        max_f16 = np.finfo('float16').max

        has_nan = len([d for d in dense if np.isnan(d)]) > 0

        if (
            not has_nan and
            max_dense > min_f16 and max_dense < max_f16 and
            min_dense > min_f16 and min_dense < max_f16
        ):
            tile_value = {
                'dense': base64.b64encode(dense.astype('float16')).decode('utf-8'),
                'dtype': 'float16'
            }
        else:
            tile_value = {
                'dense': base64.b64encode(dense.astype('float32')).decode('utf-8'),
                'dtype': 'float32'
            }

    elif tileset.filetype == 'beddb':
        tile_position = list(map(int, tile_id_parts[1:3]))
        tile_value = cdt.get_tile(
            get_datapath(tileset.datafile.url),
            tile_position[0],
            tile_position[1]
        )

    elif tileset.filetype == 'bed2ddb':
        tile_position = list(map(int, tile_id_parts[1:4]))
        tile_value = cdt.get_2d_tile(
            get_datapath(tileset.datafile.url),
            tile_position[0],
            tile_position[1],
            tile_position[2]
        )

    elif tileset.filetype == 'hibed':
        tile_position = list(map(int, tile_id_parts[1:3]))
        dense = hdft.get_discrete_data(
            h5py.File(
                get_datapath(tileset.datafile.url)
            ),
            tile_position[0],
            tile_position[1]
        )

        tile_value = {'discrete': list([list([x.decode('utf-8') for x in d]) for d in dense])}
    elif tileset.filetype == "cooler":
        tile_position = list(map(int, tile_id_parts[1:4]))


        if len(tile_id_parts) > 4:
            transform_method = tile_id_parts[4]
        else:
            transform_method = 'default'
        
        tile_value = make_cooler_tile(
            get_datapath(tileset.datafile.url), tile_position,
            transform_method
        )
        if tile_value is None:
            return None

    rdb.set(tile_id, pickle.dumps(tile_value))
    return (tile_id, tile_value)


class UserList(generics.ListAPIView):
    queryset = User.objects.all()
    serializer_class = tss.UserSerializer


class UserDetail(generics.RetrieveAPIView):
    queryset = User.objects.all()
    serializer_class = tss.UserSerializer

@api_view(['GET'])
def uids_by_filename(request):
    '''
    Retrieve a list uids corresponding to a given filename
    '''
    queryset = tm.Tileset.objects.all()
    queryset = queryset.filter(datafile__contains=request.GET['d'])

    serializer = tss.UserFacingTilesetSerializer(queryset, many=True)

    return JsonResponse({"count": len(queryset), "results": serializer.data})

@api_view(['GET'])
@authentication_classes((CsrfExemptSessionAuthentication, BasicAuthentication))
def available_chrom_sizes(request):
    '''
    Get the list of available chromosome size lists.

    Args:
        request: HTTP GET request object. Should contain no query features

    Returns:
        A JSON response containing the list of chromosome size lists.
    '''
    queryset = tm.Tileset.objects.all()
    queryset = queryset.filter(datatype__in=["chromsizes"])

    serializer = tss.UserFacingTilesetSerializer(queryset, many=True)

    return JsonResponse({"count": len(queryset), "results": serializer.data})


@api_view(['GET'])
@authentication_classes((CsrfExemptSessionAuthentication, BasicAuthentication))
def sizes(request):
    '''Return chromosome sizes.

    Retrieves the chromSiyes.tsv and either retrieves it as is or converts it
    to a JSON format.

    Args:
        request: HTTP GET request object. The request can feature the following
            queries:

            id: id of the stored chromSizes [e.g.: hg19 or mm9]
            type: return data format [tsv or json]
            cum: return cumulative size or offset [0 or 1]

    Returns:
        A HTTP text or JSON response depending on the GET request.

        A text response looks like this:
        ```
        chr1    1
        chr2    2
        ...
        ```

        A JSON response looks like this:
        ```
        {
            chr1: {
                size: 1,
                offset: 0
            }
            chr2: {
                size: 2,
                offset: 1
            },
            ...
        }
        ```
    '''
    uuid = request.GET.get('id', False)
    res_type = request.GET.get('type', 'tsv')
    incl_cum = request.GET.get('cum', False)

    response = HttpResponse
    is_json = False

    if res_type == 'json':
        is_json = True
        response = JsonResponse

    if res_type != 'json' and incl_cum:
        return response(
            'Sorry buddy. Cumulative sizes not yet supported for non-JSON '
            'file types. 😞', status=501
        )

    # Try to find the db entry
    try:
        chrom_sizes = tm.Tileset.objects.get(uuid=uuid)
    except Exception as e:
        logger.error(e)
        err_msg = 'Oh lord! ChromSizes for %s not found. ☹️' % uuid
        err_status = 404

        if is_json:
            return response({'error': err_msg}, status=err_status)

        return response(err_msg, status=err_status)

    # Try to load the CSV file
    try:
        f = chrom_sizes.datafile
        f.open('r')

        if res_type == 'json':
            reader = csv.reader(f, delimiter='\t')

            data = []
            for row in reader:
                data.append(row)
        else:
            data = f.readlines()

        f.close()
    except Exception as e:
        logger.error(e)
        err_msg = 'WHAT?! Could not load file %s. 😤 (%s)' % (
            chrom_sizes.datafile, e
        )
        err_status = 500

        if is_json:
            return response({'error': err_msg}, status=err_status)

        return response(err_msg, status=err_status)

    # Convert the stuff if needed
    try:
        if res_type == 'json' and not incl_cum:
            json_out = {}

            for row in data:
                json_out[row[0]] = {
                    'size': int(row[1])
                }

            data = json_out

        if res_type == 'json' and incl_cum:
            json_out = {}
            cum = 0

            for row in data:
                size = int(row[1])

                json_out[row[0]] = {
                    'size': size,
                    'offset': cum
                }
                cum += size

            data = json_out
    except Exception as e:
        logger.error(e)
        err_msg = 'THIS IS AN OUTRAGE!!!1! Something failed. 😡'
        err_status = 500

        if is_json:
            return response({'error': err_msg}, status=err_status)

        return response(err_msg, status=err_status)

    return response(data)


@api_view(['GET'])
def suggest(request):
    '''
    Suggest gene names based on the input text
    '''
    # suggestions are taken from a gene annotations tileset
    tileset_uuid = request.GET['d']
    text = request.GET['ac']

    try:
        tileset = tm.Tileset.objects.get(uuid=tileset_uuid)
    except ObjectDoesNotExist:
        raise rfe.NotFound('Suggestion source file not found')

    result_dict = tsu.get_gene_suggestions(
        get_datapath(tileset.datafile.url), text
    )

    return JsonResponse(result_dict, safe=False)


@api_view(['GET', 'POST'])
def viewconfs(request):
    '''
    Retrieve a viewconfs with a given uid

    Args:

    request (django.http.HTTPRequest): The request object containing the
        uid (e.g. d=hg45ksdjfds) that identifies the viewconf.

    Return:

    '''
    if request.method == 'POST':
        if not hss.UPLOAD_ENABLED:
            return JsonResponse({
                'error': 'Uploads disabled'
            }, status=403)

        if request.user.is_anonymous() and not hss.PUBLIC_UPLOAD_ENABLED:
            return JsonResponse({
                'error': 'Public uploads disabled'
            }, status=403)

        #print("request.body:", request.body)
        viewconf_wrapper = json.loads(request.body)
        uid = viewconf_wrapper.get('uid') or slugid.nice().decode('utf-8')

        try:
            viewconf = json.dumps(viewconf_wrapper['viewconf'])
        except KeyError:
            return JsonResponse({
                'error': 'Broken view config'
            }, status=400)

        try:
            higlass_version = viewconf_wrapper['higlassVersion']
        except KeyError:
            higlass_version = ''

        serializer = tss.ViewConfSerializer(data={'viewconf': viewconf})

        if not serializer.is_valid():
            return JsonResponse({
                'error': 'Serializer not valid'
            }, status=rfs.HTTP_400_BAD_REQUEST)

        serializer.save(
            uuid=uid, viewconf=viewconf, higlassVersion=higlass_version
        )

        return JsonResponse({'uid': uid})

    uid = request.GET.get('d')

    if not uid:
        return JsonResponse({
            'error': 'View config ID not specified'
        }, status=404)

    try:
        obj = tm.ViewConf.objects.get(uuid=uid)
    except ObjectDoesNotExist:
        return JsonResponse({
            'error': 'View config not found'
        }, status=404)

    return JsonResponse(json.loads(obj.viewconf))

def add_transform_type(tile_id): 
    '''
    Add a transform type to a cooler tile id if it's not already
    present.

    Parameters
    ----------
    tile_id: str
        A tile id (e.g. xyz.0.1.0)

    Returns
    -------
    new_tile_id: str
        A formatted tile id, potentially with an added transform_type
    '''
    tile_id_parts = tile_id.split('.')
    tileset_uuid = tile_id_parts[0]
    tile_position = tile_id_parts[1:4]

    transform_type = get_transform_type(tile_id)
    new_tile_id = ".".join([tileset_uuid] + tile_position + [transform_type])
    return new_tile_id

@api_view(['GET'])
def tiles(request):
    '''Retrieve a set of tiles

    A call to this API function should retrieve a few tiles.

    Args:
        request (django.http.HTTPRequest): The request object containing
            the parameters (e.g. d=x.0.0) that identify the tiles being
            requested.

    Returns:
        django.http.JsonResponse: A JSON object containing all of the tile
            data being requested. The JSON object is just a dictionary of
            (tile_id, tile_data) items.

    '''

    global mats

    # create a set so that we don't fetch the same tile multiple times
    tileids_to_fetch = set(request.GET.getlist("d"))
    # with ProcessPoolExecutor() as executor:
    # 	  res = executor.map(parallelize, hargs)
    '''
    p = mp.Pool(4)
    res = p.map(parallelize, hargs)
    '''

    tileids_by_tileset = col.defaultdict(set)
    generated_tiles = []

    tilesets = {}
    transform_id_to_original_id = {}

    # sort tile_ids by the dataset they come from
    for tile_id in tileids_to_fetch:
        tileset_uuid = extract_tileset_uid(tile_id)

        # get the tileset object first
        if tileset_uuid in tilesets:
            tileset = tilesets[tileset_uuid]
        else:
            tileset = tm.Tileset.objects.get(uuid=tileset_uuid)
            tilesets[tileset_uuid] = tileset

        if tileset.filetype == 'cooler':
            new_tile_id = add_transform_type(tile_id)
            transform_id_to_original_id[new_tile_id] = tile_id
            tile_id = new_tile_id
        else:
            transform_id_to_original_id[tile_id] = tile_id

        # see if the tile is cached
        tile_value = rdb.get(tile_id)

        if tile_value is not None:
            # we found the tile in the cache, no need to fetch it again
            tile_value = pickle.loads(tile_value)
            generated_tiles += [(tile_id, tile_value)]
            continue
            
        print("adding", tile_id)
        tileids_by_tileset[tileset_uuid].add(tile_id)

    # fetch the tiles
    for tileset_uuid in tileids_by_tileset:
        # load the tileset object
        tileset = tilesets[tileset_uuid]

        # check permissions
        if tileset.private and request.user != tileset.owner:
            generated_tiles += [(tile_id, {'error': "Forbidden"}) for tile_id in tileids_by_tileset[tileset_uuid]]
        else:
            generated_tiles += generate_tiles(tileset, tileids_by_tileset[tileset_uuid])

    # store the tiles in redis

    tiles_to_return = {}

    for (tile_id, tile_value) in generated_tiles:
        rdb.set(tile_id, pickle.dumps(tile_value))

        print("transform_id_to_original_id:", transform_id_to_original_id)
        original_tile_id = transform_id_to_original_id[tile_id]

        if original_tile_id in tileids_to_fetch:
            tiles_to_return[original_tile_id] = tile_value

    print("ttr:", tiles_to_return.keys())
    return JsonResponse(tiles_to_return, safe=False)


def get_datapath(relpath):
    return op.join(hss.BASE_DIR, relpath)


@api_view(['GET'])
def tileset_info(request):
    ''' Get information about a tileset

    Tilesets have information critical to their display
    such as the maximum number of dimensions and well as
    their width. This needs to be relayed to the client
    in order for it to know which tiles to request.

    Args:
        request (django.http.HTTPRequest): The request object
            containing tileset_ids in the 'd' parameter.
    Return:
        django.http.JsonResponse: A JSON object containing
            the tileset meta-information
    '''
    global mats
    queryset = tm.Tileset.objects.all()
    tileset_uuids = request.GET.getlist("d")
    tileset_infos = {}
    for tileset_uuid in tileset_uuids:
        tileset_object = queryset.filter(uuid=tileset_uuid).first()

        if tileset_object is None:
            tileset_infos[tileset_uuid] = {
                'error': 'No such tileset with uid: {}'.format(tileset_uuid)
            }
            continue

        if tileset_object.private and request.user != tileset_object.owner:
            # dataset is not public
            tileset_infos[tileset_uuid] = {'error': "Forbidden"}
            continue

        if (
            tileset_object.filetype == 'hitile' or
            tileset_object.filetype == 'hibed'
        ):
            tileset_info = hdft.get_tileset_info(
                h5py.File(get_datapath(tileset_object.datafile.url)))
            tileset_infos[tileset_uuid] = {
                "min_pos": [int(tileset_info['min_pos'])],
                "max_pos": [int(tileset_info['max_pos'])],
                "max_width": 2 ** math.ceil(
                    math.log(
                        tileset_info['max_pos'] - tileset_info['min_pos']
                    ) / math.log(2)
                ),
                "tile_size": int(tileset_info['tile_size']),
                "max_zoom": int(tileset_info['max_zoom'])
            }
        elif tileset_object.filetype == "elastic_search":
            response = urllib.urlopen(
                tileset_object.datafile + "/tileset_info")
            tileset_infos[tileset_uuid] = json.loads(response.read())
        elif tileset_object.filetype == 'beddb':
            tileset_infos[tileset_uuid] = cdt.get_tileset_info(
                get_datapath(tileset_object.datafile.url)
            )
        elif tileset_object.filetype == 'bed2ddb':
            tileset_infos[tileset_uuid] = cdt.get_2d_tileset_info(
                get_datapath(tileset_object.datafile.url)
            )
        elif tileset_object.filetype == 'cooler':
            dsetname = get_datapath(queryset.filter(
                uuid=tileset_uuid
            ).first().datafile.url)

            if dsetname not in mats:
                make_mats(dsetname)
            tileset_infos[tileset_uuid] = mats[dsetname][1]
        else:
            # Unknown filetype
            tileset_infos[tileset_uuid] = {
                'message': 'Unknown filetype ' + tileset_object.filetype
            }

        tileset_infos[tileset_uuid]['name'] = tileset_object.name
        tileset_infos[tileset_uuid]['coordSystem'] = tileset_object.coordSystem
        tileset_infos[tileset_uuid]['coordSystem2'] =\
            tileset_object.coordSystem2

    '''
    for info in tileset_infos.values():
        print('info:', info, type(info['max_width']), [type(x) for x in info['max_pos']], [type(x) for x in info['min_pos']], type(info['tile_size']), type(info['max_zoom']))
    '''

    return JsonResponse(tileset_infos)


@method_decorator(gzip_page, name='dispatch')
class TilesetsViewSet(viewsets.ModelViewSet):
    """Tilesets"""

    queryset = tm.Tileset.objects.all()
    serializer_class = tss.TilesetSerializer

    if hss.UPLOAD_ENABLED:
        permission_classes = (tsp.UserPermission,)
    else:
        permission_classes = (tsp.UserPermissionReadOnly,)

    lookup_field = 'uuid'
    parser_classes = (rfp.MultiPartParser,)

    def list(self, request, *args, **kwargs):
        '''List the available tilesets

        Args:
            request (django.http.HTTPRequest): The HTTP request containing
                no parameters

        Returns:
            django.http.JsonResponse: A json file containing a 'count' as
                well as 'results' with each tileset as an entry
        '''
        # only return tilesets which are accessible by this user
        if request.user.is_anonymous:
            user = gu.get_anonymous_user()
        else:
            user = request.user

        queryset = self.queryset.filter(
            dbm.Q(owner=user) | dbm.Q(private=False)
        )

        if 'ac' in request.GET:
            # Autocomplete fields
            queryset = queryset.filter(name__contains=request.GET['ac'])
        if 't' in request.GET:
            # Filter by filetype
            queryset = queryset.filter(filetype=request.GET['t'])
        if 'dt' in request.GET:
            # Filter by datatype
            queryset = queryset.filter(datatype__in=request.GET.getlist('dt'))

        if 'o' in request.GET:
            if 'r' in request.GET:
                queryset = queryset.order_by(dbmf.Lower(request.GET['o']).desc())
            else:
                queryset = queryset.order_by(dbmf.Lower(request.GET['o']).asc())

        #ts_serializer = tss.UserFacingTilesetSerializer(queryset, many=True)
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = ts_serializer(queryset, many=True)
        return JsonResponse(serializer.data)

        """
        return JsonResponse(
            {"count": len(queryset), "results": ts_serializer.data}
        )
        """

    def perform_create(self, serializer):
        '''Add a new tileset

        When adding a new dataset, we need to enforce permissions as well as
        other rules like the uniqueness of uuids.

        Args:
            serializer (tilsets.serializer.TilesetSerializer): The serializer
            to use to save the request.
        '''

        if 'uid' in self.request.data:
            try:
                self.queryset.get(uuid=self.request.data['uid'])
                # this uid already exists, return an error
                raise rfe.APIException("UID already exists")
            except tm.Tileset.DoesNotExist:
                uid = self.request.data['uid']
        else:
            uid = slugid.nice().decode('utf-8')

        if 'filetype' not in self.request.data:
            raise rfe.APIException('Missing filetype')

        datafile_name = self.request.data.get('datafile').name

        if 'name' in self.request.data:
            name = self.request.data['name']
        else:
            name = op.split(datafile_name)[1]

        if self.request.user.is_anonymous:
            # can't create a private dataset as an anonymous user
            serializer.save(
                owner=gu.get_anonymous_user(),
                private=False,
                name=name,
                uuid=uid
            )
        else:
            serializer.save(owner=self.request.user, name=name, uuid=uid)
