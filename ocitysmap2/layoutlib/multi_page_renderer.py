# -*- coding: utf-8 -*-

# ocitysmap, city map and street index generator from OpenStreetMap data
# Copyright (C) 2010  David Decotigny
# Copyright (C) 2010  Frédéric Lehobey
# Copyright (C) 2010  Pierre Mauduit
# Copyright (C) 2010  David Mentré
# Copyright (C) 2010  Maxime Petazzoni
# Copyright (C) 2010  Thomas Petazzoni
# Copyright (C) 2010  Gaël Utard

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import logging
import tempfile
import math
import sys
import cairo
try:
    import mapnik2 as mapnik
except ImportError:
    import mapnik
import coords
import locale
import pangocairo
import pango
import datetime

from itertools import groupby

from abstract_renderer import Renderer

from ocitysmap2.maplib.map_canvas import MapCanvas
from ocitysmap2.maplib.grid import Grid
from ocitysmap2.maplib.overview_grid import OverviewGrid
from indexlib.indexer import StreetIndex
from indexlib.multi_page_renderer import MultiPageStreetIndexRenderer

import ocitysmap2
import commons
import shapely.wkt
from ocitysmap2 import maplib

from indexlib.commons import IndexCategory

LOG = logging.getLogger('ocitysmap')
PAGE_STR = " - Page %(page_number)d"

_MAPNIK_PROJECTION = "+proj=merc +a=6378137 +b=6378137 +lat_ts=0.0 " \
                     "+lon_0=0.0 +x_0=0.0 +y_0=0 +k=1.0 +units=m   " \
                     "+nadgrids=@null +no_defs +over"

class MultiPageRenderer(Renderer):
    """
    This Renderer creates a multi-pages map, with all the classic overlayed
    features and no index page.
    """

    name = 'multi_page'
    description = 'A multi-page layout.'
    multipages = True

    def __init__(self, db, rc, tmpdir, dpi, street_index):
        Renderer.__init__(self, db, rc, tmpdir, dpi, street_index)

        self._grid_legend_margin_pt = \
            min(Renderer.GRID_LEGEND_MARGIN_RATIO * self.paper_width_pt,
                Renderer.GRID_LEGEND_MARGIN_RATIO * self.paper_height_pt)

        # Compute the usable area per page
        self._usable_area_width_pt = (self.paper_width_pt -
                                      (2 * Renderer.PRINT_SAFE_MARGIN_PT))
        self._usable_area_height_pt = (self.paper_height_pt -
                                       (2 * Renderer.PRINT_SAFE_MARGIN_PT))

        scale_denom = 10000
        GRAYED_MARGIN_MM  = 10
        OVERLAP_MARGIN_MM = 20

        # Debug: show original bounding box as JS code
        # print self.rc.bounding_box.as_javascript("original", "#00ff00")

        # Convert the original Bounding box into Mercator meters
        self._proj = mapnik.Projection(_MAPNIK_PROJECTION)
        orig_envelope = self._project_envelope(self.rc.bounding_box)

        # Extend the bounding box to take into account the lost outter
        # margin
        off_x  = orig_envelope.minx - (GRAYED_MARGIN_MM * scale_denom) / 1000
        off_y  = orig_envelope.miny - (GRAYED_MARGIN_MM * scale_denom) / 1000
        width  = orig_envelope.width() + (2 * GRAYED_MARGIN_MM * scale_denom) / 1000
        height = orig_envelope.height() + (2 * GRAYED_MARGIN_MM * scale_denom) / 1000

        # Calculate the total width and height of paper needed to
        # render the geographical area at the current scale.
        total_width_pt   = commons.convert_mm_to_pt(float(width) * 1000 / scale_denom)
        total_height_pt  = commons.convert_mm_to_pt(float(height) * 1000 / scale_denom)
        self.grayed_margin_pt = commons.convert_mm_to_pt(GRAYED_MARGIN_MM)
        overlap_margin_pt = commons.convert_mm_to_pt(OVERLAP_MARGIN_MM)

        # Calculate the number of pages needed in both directions
        if total_width_pt < self._usable_area_width_pt:
            nb_pages_width = 1
        else:
            nb_pages_width = \
                (float(total_width_pt - self._usable_area_width_pt) / \
                     (self._usable_area_width_pt - overlap_margin_pt)) + 1

        if total_height_pt < self._usable_area_height_pt:
            nb_pages_height = 1
        else:
            nb_pages_height = \
                (float(total_height_pt - self._usable_area_height_pt) / \
                     (self._usable_area_height_pt - overlap_margin_pt)) + 1

        # Round up the number of pages needed so that we have integer
        # number of pages
        nb_pages_width = int(math.ceil(nb_pages_width))
        nb_pages_height = int(math.ceil(nb_pages_height))

        # Calculate the entire paper area available
        total_width_pt_after_extension = \
            self._usable_area_width_pt + (self._usable_area_width_pt - overlap_margin_pt) * (nb_pages_width - 1)
        total_height_pt_after_extension = \
            self._usable_area_height_pt + (self._usable_area_height_pt - overlap_margin_pt) * (nb_pages_height - 1)

        # Convert this paper area available in the number of Mercator
        # meters that can be rendered on the map
        total_width_merc = \
            commons.convert_pt_to_mm(total_width_pt_after_extension) * scale_denom / 1000
        total_height_merc = \
            commons.convert_pt_to_mm(total_height_pt_after_extension) * scale_denom / 1000

        # Extend the geographical boundaries so that we completely
        # fill the available paper size. We are careful to extend the
        # boundaries evenly on all directions (so the center of the
        # previous boundaries remain the same as the new one)
        off_x -= (total_width_merc - width) / 2
        width = total_width_merc
        off_y -= (total_height_merc - height) / 2
        height = total_height_merc

        # Calculate what is the final global bounding box that we will render
        envelope = mapnik.Box2d(off_x, off_y, off_x + width, off_y + height)
        self._geo_bbox = self._inverse_envelope(envelope)

        # Debug: show transformed bounding box as JS code
        # print self._geo_bbox.as_javascript("extended", "#0f0f0f")

        # Convert the usable area on each sheet of paper into the
        # amount of Mercator meters we can render in this area.
        usable_area_merc_m_width  = commons.convert_pt_to_mm(self._usable_area_width_pt) * scale_denom / 1000
        usable_area_merc_m_height = commons.convert_pt_to_mm(self._usable_area_height_pt) * scale_denom / 1000
        grayed_margin_merc_m      = (GRAYED_MARGIN_MM * scale_denom) / 1000
        overlap_margin_merc_m     = (OVERLAP_MARGIN_MM * scale_denom) / 1000

        # Calculate all the bounding boxes that correspond to the
        # geographical area that will be rendered on each sheet of
        # paper.
        bboxes = []
        for j in reversed(range(0, nb_pages_height)):
            for i in range(0, nb_pages_width):
                cur_x = off_x + i * (usable_area_merc_m_width - overlap_margin_merc_m)
                cur_y = off_y + j * (usable_area_merc_m_height - overlap_margin_merc_m)
                envelope = mapnik.Box2d(cur_x, cur_y,
                                        cur_x+usable_area_merc_m_width,
                                        cur_y+usable_area_merc_m_height)

                envelope_inner = mapnik.Box2d(cur_x + grayed_margin_merc_m,
                                              cur_y + grayed_margin_merc_m,
                                              cur_x + usable_area_merc_m_width  - grayed_margin_merc_m,
                                              cur_y + usable_area_merc_m_height - grayed_margin_merc_m)

                bboxes.append((self._inverse_envelope(envelope),
                               self._inverse_envelope(envelope_inner)))

        # Debug: show per-page bounding boxes as JS code
        # for i, (bb, bb_inner) in enumerate(bboxes):
        #    print bb.as_javascript(name="p%d" % i)

        self.pages = []

        # Create an overview map

        overview_bb = self._geo_bbox.create_expanded(0.001, 0.001)
        # Create the grid
        map_grid = OverviewGrid(overview_bb,
                     [bb for bb, bb_inner in bboxes], self.rc.i18n.isrtl())

        grid_shape = map_grid.generate_shape_file(
                    os.path.join(self.tmpdir, 'grid_overview.shp'))

        # Create one canvas for the current page
        map_canvas = MapCanvas(self.rc.stylesheet,
                               overview_bb, self._usable_area_width_pt,
                               self._usable_area_height_pt, dpi,
                               extend_bbox_to_ratio=True)

        # Create the gray shape around the map
        exterior = shapely.wkt.loads(map_canvas.get_actual_bounding_box()\
                                                                .as_wkt())
        interior = shapely.wkt.loads(self.rc.polygon_wkt)
        shade_wkt = exterior.difference(interior).wkt
        shade = maplib.shapes.PolyShapeFile(self.rc.bounding_box,
                os.path.join(self.tmpdir, 'shape_overview.shp'),
                             'shade-overview')
        shade.add_shade_from_wkt(shade_wkt)


        map_canvas.add_shape_file(shade)
        map_canvas.add_shape_file(grid_shape,
                                  self.rc.stylesheet.grid_line_color,
                                  self.rc.stylesheet.grid_line_alpha,
                                  self.rc.stylesheet.grid_line_width)

        map_canvas.render()
        self.pages.append((map_canvas, map_grid))

        # Create the map canvas for each page
        indexes = []
        for i, (bb, bb_inner) in enumerate(bboxes):

            # Create the gray shape around the map
            exterior = shapely.wkt.loads(bb.as_wkt())
            interior = shapely.wkt.loads(bb_inner.as_wkt())
            shade_wkt = exterior.difference(interior).wkt
            shade = maplib.shapes.PolyShapeFile(
                bb, os.path.join(self.tmpdir, 'shape%d.shp' % i),
                'shade%d' % i)
            shade.add_shade_from_wkt(shade_wkt)

            # Create the grid
            map_grid = Grid(bb_inner, self.rc.i18n.isrtl())
            grid_shape = map_grid.generate_shape_file(
                os.path.join(self.tmpdir, 'grid%d.shp' % i))

            # Create one canvas for the current page
            map_canvas = MapCanvas(self.rc.stylesheet,
                                   bb, self._usable_area_width_pt,
                                   self._usable_area_height_pt, dpi,
                                   extend_bbox_to_ratio=False)

            map_canvas.add_shape_file(shade)
            map_canvas.add_shape_file(grid_shape,
                                      self.rc.stylesheet.grid_line_color,
                                      self.rc.stylesheet.grid_line_alpha,
                                      self.rc.stylesheet.grid_line_width)

            map_canvas.render()
            self.pages.append((map_canvas, map_grid))

            # Create the index for the current page
            index = StreetIndex(self.db,
                                bb_inner.as_wkt(),
                                self.rc.i18n, page_number=(i + 1))

            index.apply_grid(map_grid)
            indexes.append(index)

        # Merge all indexes
        self.index_categories = self._merge_page_indexes(indexes)

        # Prepare the small map for the front page
        self._front_page_map = self._prepare_front_page_map(dpi)

    def _merge_page_indexes(self, indexes):
        # First, we split street categories and "other" categories,
        # because we sort them and we don't want to have the "other"
        # categories intermixed with the street categories. This
        # sorting is required for the groupby Python operator to work
        # properly.
        all_categories_streets = []
        all_categories_others  = []
        for page_number, idx in enumerate(indexes):
            for cat in idx.categories:
                # Split in two lists depending on the category type
                # (street or other)
                if cat.is_street:
                    all_categories_streets.append(cat)
                else:
                    all_categories_others.append(cat)

        all_categories_streets_merged = \
            self._merge_index_same_categories(all_categories_streets, is_street=True)
        all_categories_others_merged = \
            self._merge_index_same_categories(all_categories_others, is_street=False)

        all_categories_merged = \
            all_categories_streets_merged + all_categories_others_merged

        return all_categories_merged

    def _merge_index_same_categories(self, categories, is_street=True):
        # Sort by categories. Now we may have several consecutive
        # categories with the same name (i.e category for letter 'A'
        # from page 1, category for letter 'A' from page 3).
        categories.sort(key=lambda s:s.name)

        categories_merged = []
        for category_name,grouped_categories in groupby(categories,
                                                        key=lambda s:s.name):

            # Group the different IndexItem from categories having the
            # same name. The groupby() function guarantees us that
            # categories with the same name are grouped together in
            # grouped_categories[].

            grouped_items = []
            for cat in grouped_categories:
                grouped_items.extend(cat.items)

            # Re-sort alphabetically all the IndexItem according to
            # the street name.

            prev_locale = locale.getlocale(locale.LC_COLLATE)
            locale.setlocale(locale.LC_COLLATE, self.rc.i18n.language_code())
            try:
                grouped_items_sorted = \
                    sorted(grouped_items,
                           lambda x,y: locale.strcoll(x.label, y.label))
            finally:
                locale.setlocale(locale.LC_COLLATE, prev_locale)

            # Rebuild a IndexCategory object with the list of merged
            # and sorted IndexItem
            categories_merged.append(
                IndexCategory(category_name, grouped_items_sorted, is_street))

        return categories_merged

    def _project_envelope(self, bbox):
        """Project the given bounding box into the rendering projection."""
        envelope = mapnik.Box2d(bbox.get_top_left()[1],
                                bbox.get_top_left()[0],
                                bbox.get_bottom_right()[1],
                                bbox.get_bottom_right()[0])
        c0 = self._proj.forward(mapnik.Coord(envelope.minx, envelope.miny))
        c1 = self._proj.forward(mapnik.Coord(envelope.maxx, envelope.maxy))
        return mapnik.Box2d(c0.x, c0.y, c1.x, c1.y)

    def _inverse_envelope(self, envelope):
        """Inverse the given cartesian envelope (in 900913) back to a 4002
        bounding box."""
        c0 = self._proj.inverse(mapnik.Coord(envelope.minx, envelope.miny))
        c1 = self._proj.inverse(mapnik.Coord(envelope.maxx, envelope.maxy))
        return coords.BoundingBox(c0.y, c0.x, c1.y, c1.x)

    def _prepare_front_page_map(self, dpi):
        front_page_map_w = \
            self._usable_area_width_pt - 2 * Renderer.PRINT_SAFE_MARGIN_PT
        front_page_map_h = \
            (self._usable_area_height_pt - 2 * Renderer.PRINT_SAFE_MARGIN_PT) / 2

        # Create the nice small map
        front_page_map = \
            MapCanvas(self.rc.stylesheet,
                      self.rc.bounding_box,
                      front_page_map_w,
                      front_page_map_h,
                      dpi,
                      extend_bbox_to_ratio=True)

        # Add the shape that greys out everything that is outside of
        # the administrative boundary.
        exterior = shapely.wkt.loads(front_page_map.get_actual_bounding_box().as_wkt())
        interior = shapely.wkt.loads(self.rc.polygon_wkt)
        shade_wkt = exterior.difference(interior).wkt
        shade = maplib.shapes.PolyShapeFile(self.rc.bounding_box,
                os.path.join(self.tmpdir, 'shape_overview_cover.shp'),
                             'shade-overview-cover')
        shade.add_shade_from_wkt(shade_wkt)
        front_page_map.add_shape_file(shade)
        front_page_map.render()
        return front_page_map

    def _render_front_page_header(self, ctx, w, h):
        # Draw a light blue block which will contain the name of the
        # city being rendered.
        blue_w = w
        blue_h = 0.3 * h
        ctx.set_source_rgb(.80,.80,.80)
        ctx.rectangle(0, 0, blue_w, blue_h)
        ctx.fill()

        # Prepare the title text layout
        pc = pangocairo.CairoContext(ctx)
        layout = pc.create_layout()
        layout.set_width(int(0.7 * w * pango.SCALE))
        layout.set_alignment(pango.ALIGN_CENTER)
        fd = pango.FontDescription("Georgia Bold")
        fd.set_size(pango.SCALE)
        layout.set_font_description(fd)
        layout.set_text(self.rc.title)
        self._adjust_font_size(layout, fd, 0.7 * blue_w, 0.8 * blue_h)

        # Draw the title
        text_x, text_y, text_w, text_h = layout.get_extents()[1]
        ctx.save()
        ctx.set_source_rgb(0, 0, 0)
        ctx.translate((blue_w / 2) - (text_w / 2.0) / pango.SCALE - text_x / pango.SCALE,
                      (blue_h / 2) - (text_h / 2.0) / pango.SCALE - text_y / pango.SCALE)
        pc.show_layout(layout)
        ctx.restore()

    def _render_front_page_map(self, ctx, dpi, w, h):
        # We will render the map slightly below the title
        ctx.save()
        ctx.translate(0, 0.3 * h + Renderer.PRINT_SAFE_MARGIN_PT)

        # Render the map !
        mapnik.render(self._front_page_map.get_rendered_map(), ctx)
        ctx.restore()

    def _render_front_page_footer(self, ctx, w, h, osm_date):
        ctx.save()

        # Draw the footer
        ctx.translate(0, 0.8 * h + 2 * Renderer.PRINT_SAFE_MARGIN_PT)

        # Display a nice grey rectangle as the background of the
        # footer
        footer_w = w
        footer_h = 0.2 * h - 2 * Renderer.PRINT_SAFE_MARGIN_PT
        ctx.set_source_rgb(.80,.80,.80)
        ctx.rectangle(0, 0, footer_w, footer_h)
        ctx.fill()

        # Draw the OpenStreetMap logo to the right of the footer
        logo_height = footer_h / 2
        grp, logo_width = self._get_osm_logo(ctx, logo_height)
        if grp:
            ctx.save()
            ctx.translate(w - logo_width - Renderer.PRINT_SAFE_MARGIN_PT,
                          logo_height / 2)
            ctx.set_source(grp)
            ctx.paint_with_alpha(0.8)
            ctx.restore()

        # Prepare the text for the left of the footer
        today = datetime.date.today()
        notice = \
            _(u'Copyright © %(year)d MapOSMatic/OCitySMap developers.\n'
              u'http://www.maposmatic.org\n\n'
              u'Map data © %(year)d OpenStreetMap.org '
              u'and contributors (cc-by-sa).\n'
              u'http://www.openstreetmap.org\n\n'
              u'Map rendered on: %(date)s. OSM data updated on: %(osmdate)s.\n'
              u'The map may be incomplete or inaccurate. '
              u'You can contribute to improve this map.\n'
              u'See http://wiki.openstreetmap.org')

        # We need the correct locale to be set for strftime().
        prev_locale = locale.getlocale(locale.LC_TIME)
        locale.setlocale(locale.LC_TIME, self.rc.i18n.language_code())
        try:
            if osm_date is None:
                osm_date_str = _(u'unknown')
            else:
                osm_date_str = osm_date.strftime("%d %B %Y %H:%M")

            notice = notice % {'year': today.year,
                               'date': today.strftime("%d %B %Y"),
                               'osmdate': osm_date_str}
        finally:
            locale.setlocale(locale.LC_TIME, prev_locale)

        # Render the text
        pc = pangocairo.CairoContext(ctx)
        layout = pc.create_layout()
        layout.set_width(int(footer_w * 0.7) * pango.SCALE)
        layout.set_alignment(pango.ALIGN_LEFT)
        fd = pango.FontDescription("Georgia Bold")
        fd.set_size(pango.SCALE)
        layout.set_font_description(fd)
        layout.set_text(notice)
        self._adjust_font_size(layout, fd, footer_w * 0.7, footer_h * 0.8)

        text_x, text_y, text_w, text_h = layout.get_extents()[1]
        ctx.save()
        ctx.set_source_rgb(0,0,0)
        ctx.translate(Renderer.PRINT_SAFE_MARGIN_PT,
                      (footer_h / 2) - (text_h / 2.0 / pango.SCALE))
        # Hack to workaround what appears to be a Cairo bug: without
        # drawing a rectangle here, the translation above is not taken
        # into account for rendering the text.
        ctx.rectangle(0, 0, 0, 0)
        pc.show_layout(layout)
        ctx.restore()

        ctx.restore()

    def _render_front_page(self, ctx, cairo_surface, dpi, osm_date):
        # Draw a nice grey rectangle covering the whole page
        ctx.save()
        ctx.set_source_rgb(.95,.95,.95)
        ctx.rectangle(Renderer.PRINT_SAFE_MARGIN_PT,
                      Renderer.PRINT_SAFE_MARGIN_PT,
                      self._usable_area_width_pt,
                      self._usable_area_height_pt)
        ctx.fill()
        ctx.restore()

        # Translate into the working area, taking another
        # PRINT_SAFE_MARGIN_PT inside the grey area.
        ctx.save()
        ctx.translate(2 * Renderer.PRINT_SAFE_MARGIN_PT,
                      2 * Renderer.PRINT_SAFE_MARGIN_PT)
        w = self._usable_area_width_pt - 2 * Renderer.PRINT_SAFE_MARGIN_PT
        h = self._usable_area_height_pt - 2 * Renderer.PRINT_SAFE_MARGIN_PT

        self._render_front_page_header(ctx, w, h)
        self._render_front_page_map(ctx, dpi, w, h)
        self._render_front_page_footer(ctx, w, h, osm_date)

        ctx.restore()

        cairo_surface.show_page()

    def render(self, cairo_surface, dpi, osm_date):
        ctx = cairo.Context(cairo_surface)

        self._render_front_page(ctx, cairo_surface, dpi, osm_date)

        for i, (canvas, grid) in enumerate(self.pages):
            ctx.save()

            # Prepare to draw the map at the right location
            ctx.translate(commons.convert_pt_to_dots(Renderer.PRINT_SAFE_MARGIN_PT),
                          commons.convert_pt_to_dots(Renderer.PRINT_SAFE_MARGIN_PT))

            rendered_map = canvas.get_rendered_map()
            mapnik.render(rendered_map, ctx)

            # Render the page number
            ctx.save()
            ctx.translate(commons.convert_pt_to_dots(self._usable_area_width_pt),
                          commons.convert_pt_to_dots(self._usable_area_height_pt))
            Renderer._draw_centered_text(ctx, str(i + 1), 0, 0)
            ctx.restore()

            ctx.save()
            ctx.translate(commons.convert_pt_to_dots(self.grayed_margin_pt),
                          commons.convert_pt_to_dots(self.grayed_margin_pt))

            # Place the vertical and horizontal square labels
            self._draw_labels(ctx, grid,
                              commons.convert_pt_to_dots(self._usable_area_width_pt)  - 2 * commons.convert_pt_to_dots(self.grayed_margin_pt),
                              commons.convert_pt_to_dots(self._usable_area_height_pt) - 2 * commons.convert_pt_to_dots(self.grayed_margin_pt),
                              commons.convert_pt_to_dots(self._grid_legend_margin_pt))

            ctx.restore()

            ctx.restore()

            cairo_surface.show_page()

        mpsir = MultiPageStreetIndexRenderer(self.rc.i18n,
                                             ctx, cairo_surface,
                                             self.index_categories,
                                             (Renderer.PRINT_SAFE_MARGIN_PT,
                                              Renderer.PRINT_SAFE_MARGIN_PT,
                                              self._usable_area_width_pt,
                                              self._usable_area_height_pt))

        mpsir.render()

        cairo_surface.flush()

    # Convert a length in geometric meters (in the real life) into a
    # length in paper millimiters (as drawn on the map).
    def _geo_m_to_paper_mm(self, geo_m):
        return geo_m / 1000.0 * Renderer.DEFAULT_KM_IN_MM * 2

    def _paper_mm_to_geo_m(self, paper_mm):
        return paper_mm * 1000.0 / (Renderer.DEFAULT_KM_IN_MM * 2)

    def _paper_pt_to_geo_m(self, paper_pt):
        return self._paper_mm_to_geo_m(commons.convert_pt_to_mm(paper_pt))

    # In multi-page mode, we only accept A4, A5 and US letter as paper
    # sizes. The goal is to render booklets, not posters.
    @staticmethod
    def get_compatible_paper_sizes(bounding_box, zoom_level,
                                   resolution_km_in_mm=Renderer.DEFAULT_KM_IN_MM,
                                   index_position=None, hsplit=1, vsplit=1):
        valid_sizes = []
        acceptable_formats = [ 'A5', 'A4', 'US letter' ]
        for sz in ocitysmap2.layoutlib.PAPER_SIZES:
            # Skip unsupported paper formats
            if sz[0] not in acceptable_formats:
                continue
            valid_sizes.append((sz[0], sz[1], sz[2], True, True))
        return valid_sizes

