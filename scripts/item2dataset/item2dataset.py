import stac
from collections import OrderedDict
import yaml
import click
from urllib.parse import urlparse
from osgeo import osr
from osgeo import gdal
import uuid
from datetime import datetime

import osgeo

# Fontes:
# https://github.com/opendatacube/datacube-core/blob/datacube-1.7/docs/ops/dataset_documents.rst
# https://github.com/opendatacube/datacube-core/blob/0650038d113f8d7f5f4a47075eea706fe24a84fb/docs/config_samples/match_rules/ls5_scenes.yaml
# https://github.com/opendatacube/datacube-core/blob/c4595b91387011d4deec3c2d63f88e9e9aa01573/datacube/index/default-metadata-types.yaml
#


def setup_yaml():
    """ https://stackoverflow.com/a/8661021 """

    def represent_dict_order(self, data): return self.represent_mapping(
        'tag:yaml.org,2002:map', data.items())
    yaml.add_representer(OrderedDict, represent_dict_order)


setup_yaml()


def href_to_path(href, basepath):
    url = urlparse(href)
    return "{}{}".format(basepath, url.path)


def lon_lat_2_y_x(geo_ref_points):
    def transform(p):
        return {'x': p['lon'], 'y': p['lat']}

    return {key: transform(p) for key, p in geo_ref_points.items()}


def generate_id(feature):
    for link in feature['links']:
        if link['rel'] == 'self':
            return str(uuid.uuid5(uuid.NAMESPACE_URL, link['href']))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, feature['id']))


def generate_product_type(collection):
    return "{}_{}_{}".format(
        collection['properties']['bdc:temporal_composition']['schema'],
        collection['properties']['bdc:temporal_composition']['step'],
        collection['properties']['bdc:temporal_composition']['unit'])


def convert_coords(coords, in_spatial_ref, out_spatial_ref):

    t = osr.CoordinateTransformation(in_spatial_ref, out_spatial_ref)

    if int(osgeo.__version__[0]) >= 3:
        # GDAL 3 changes axis order: https://github.com/OSGeo/gdal/issues/1546
        in_spatial_ref.SetAxisMappingStrategy(
            osgeo.osr.OAMS_TRADITIONAL_GIS_ORDER)
        out_spatial_ref.SetAxisMappingStrategy(
            osgeo.osr.OAMS_TRADITIONAL_GIS_ORDER)

    def transform(p):
        a = t.TransformPoint(p['lon'], p['lat'])
        return {'lon': a[0], 'lat': a[1]}

    return {key: transform(p) for key, p in coords.items()}


def convert_coords_xy(coords, in_spatial_ref, out_spatial_ref):
    t = osr.CoordinateTransformation(in_spatial_ref, out_spatial_ref)

    def transform(p):
        a = t.TransformPoint(p['x'], p['y'])
        return {'x': a[0], 'y': a[1]}

    return {key: transform(p) for key, p in coords.items()}


def convert_bdc_item(collection, constants):
    crs_proj4 = collection['properties']['bdc:crs']
    sr = osr.SpatialReference()
    sr.ImportFromProj4(crs_proj4)
    crs_wkt = sr.ExportToWkt()

    out_spatial_ref = osr.SpatialReference()

    out_spatial_ref.ImportFromProj4(crs_proj4)

    in_spatial_ref = osr.SpatialReference()
    in_spatial_ref.ImportFromEPSG(4326)

    product_type = generate_product_type(collection)

    limit = 120
    page = 1
    max_page = 99999999
    total_items = 0
    max_items = constants['max_items']

    for page in range(1, max_page+1):

        if max_items is not None:
            if max_items == total_items:
                break

        if limit > (max_items-total_items):
            limit = (max_items-total_items)

        features = collection.get_items(
            filter={'page': page, 'limit': limit}).features

        if len(features) == 0:
            break

        for f in features:
            datetime_object = datetime.strptime(
                f['properties']['datetime'], '%Y-%m-%d')
            datetime_str = datetime_object.strftime("%Y-%m-%d %H:%M:%S.%fZ")

            feature = OrderedDict()
            feature['id'] = generate_id(f)
            feature['creation_dt'] = datetime_str
            feature['product_type'] = product_type
            feature['platform'] = {'code': constants['plataform_code']}
            feature['instrument'] = {'name': collection['id']}
            feature['format'] = {'name': constants['format_name']}
            feature['lineage'] = {'source_datasets': {}}

            feature['extent'] = OrderedDict()
            feature['extent']['coord'] = OrderedDict()
            feature['extent']['coord']['ul'] = {'lon': f['geometry']['coordinates'][0][0][0],
                                                'lat': f['geometry']['coordinates'][0][0][1]}
            feature['extent']['coord']['ur'] = {'lon': f['geometry']['coordinates'][0][1][0],
                                                'lat': f['geometry']['coordinates'][0][1][1]}
            feature['extent']['coord']['lr'] = {'lon': f['geometry']['coordinates'][0][2][0],
                                                'lat': f['geometry']['coordinates'][0][2][1]}
            feature['extent']['coord']['ll'] = {'lon': f['geometry']['coordinates'][0][3][0],
                                                'lat': f['geometry']['coordinates'][0][3][1]}

            ####
            feature['extent']['from_dt'] = datetime_str
            feature['extent']['center_dt'] = datetime_str
            feature['extent']['to_dt'] = datetime_str

            # Extract image bbox
            first_band = next(iter(collection['properties']['bdc:bands']))
            first_band_path = href_to_path(
                f['assets'][first_band]['href'], constants['basepath'])

            src = gdal.Open(first_band_path)
            ulx, xres, _, uly, _, yres = src.GetGeoTransform()
            lrx = ulx + (src.RasterXSize * xres)
            lry = uly + (src.RasterYSize * yres)

            feature['grid_spatial'] = OrderedDict()
            feature['grid_spatial']['projection'] = OrderedDict()
            feature['grid_spatial']['projection']['geo_ref_points'] = OrderedDict()
            feature['grid_spatial']['projection']['geo_ref_points']['ul'] = {
                'x': ulx, 'y': uly}
            feature['grid_spatial']['projection']['geo_ref_points']['ur'] = {
                'x': lrx, 'y': uly}
            feature['grid_spatial']['projection']['geo_ref_points']['lr'] = {
                'x': lrx, 'y': lry}
            feature['grid_spatial']['projection']['geo_ref_points']['ll'] = {
                'x': ulx, 'y': lry}

            feature['grid_spatial']['projection']['spatial_reference'] = crs_wkt
            feature['image'] = OrderedDict()
            feature['image']['bands'] = OrderedDict()
            band_counter = 1
            for band in collection['properties']['bdc:bands'].keys():
                if band not in constants['ignore']:
                    if band in f['assets']:
                        feature['image']['bands'][band] = OrderedDict()
                        feature['image']['bands'][band]['path'] = href_to_path(
                            f['assets'][band]['href'], constants['basepath'])
                        feature['image']['bands'][band]['layer'] = 1
                        band_counter += 1
                    else:
                        print("Band '{}' was not found in asset '{}'".format(
                            band, f['id']))
            file_name = "{}{}.yaml".format(constants['outpath'], f['id'])
            with open(file_name, 'w') as f:
                yaml.dump(feature, f)
            total_items += 1


@click.command()
@click.option('-c', '--collection', required=True, help='Collection name (Ex. C4_64_16D_MED).')
@click.option('-t', '--type', default='eo', help='Metadata type.')
@click.option('-p', '--code', default='BDC', help='Plataform code.')
@click.option('-f', '--format', default='GeoTiff', help='Format name.')
@click.option('--units', default='1', help='Units.')
@click.option('--url', default='http://brazildatacube.dpi.inpe.br/bdc-stac/0.8.0/', help='BDC STAC url.')
@click.option('--basepath', default='/gfs', help='Repository base path')
@click.option('-o', '--outpath', default='./', help='Output path')
@click.option('-i', '--ignore', default=['quality'], help='List of bands to ignore')
@click.option('-m', '--max_items', default=None, help='Max items')
def main(collection, type, code, format, units, url, basepath, outpath, ignore, max_items):
    constants = {
        'metadata_type': type,
        'plataform_code': code,
        'format_name': format,
        'units': units,
        'basepath': basepath,
        'ignore': ignore,
        'outpath': outpath,
        'max_items': int(max_items)
    }
    s = stac.STAC(url, True)
    c = s.collection(collection)
    convert_bdc_item(c, constants)


if __name__ == '__main__':
    main()
