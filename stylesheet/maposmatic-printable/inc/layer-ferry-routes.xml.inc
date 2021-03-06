<Style name="ferry-routes">
    <Rule>
      &maxscale_zoom7;
      &minscale_zoom10;
      <LineSymbolizer stroke="#66f" stroke-width="0.4" stroke-dasharray="4,4"/>
    </Rule>
    <Rule>
      &maxscale_zoom11;
      <LineSymbolizer stroke="#66f" stroke-width="0.8" stroke-dasharray="6,6"/>
    </Rule>
</Style>

<Layer name="ferry-routes" status="on" srs="&osm2pgsql_projection;">
    <StyleName>ferry-routes</StyleName>
    <Datasource>
      <Parameter name="table">
      (select way from &prefix;_line where route='ferry' ) as routes
      </Parameter>
      &datasource-settings;
    </Datasource>
</Layer>
