/**
 * reports-charts.js — Shared chart rendering utilities for the INSYTE reports dashboard.
 *
 * Exposes the following globals used by both the full-page (reports.html) and
 * the HTMX partial (results_panel.html) rendering paths:
 *
 *   window._palette              – shared ApexCharts colour palette
 *   window._fmtGBP(v)            – "£X,XXX.XX" formatter
 *   window._fmtGBPShort(v)       – "£X,XXX" formatter
 *   window._fmtPct(v)            – "X.X%" formatter
 *   window.renderDashboard(data) – renders the All Donations multi-widget dashboard
 *   window.initLeafletMap(id, d) – renders the UK regional Leaflet choropleth
 *
 * The full-page and HTMX paths both load this file.  Each path then defines its own
 * inline renderReportChart() that injects {{ chart_data_json|safe }} and calls
 * renderDashboard() or the appropriate single-chart setup from here.
 */

/* ── Colour palette ──────────────────────────────────────────────────────── */
window._fontSans = 'IBM Plex Sans, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif';
window.Apex = window.Apex || {};
window.Apex.chart = Object.assign({}, window.Apex.chart, { fontFamily: window._fontSans });

window._palette = [
    '#3B82F6', '#10B981', '#F59E0B', '#EF4444', '#8B5CF6',
    '#EC4899', '#06B6D4', '#F97316', '#14B8A6', '#6366F1',
    '#D946EF', '#84CC16',
];

/* ── Currency & percentage formatters ────────────────────────────────────── */
window._fmtGBP = function (v) {
    return '£' + Number(v).toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

window._fmtGBPShort = function (v) {
    return '£' + Number(v).toLocaleString('en-GB');
};

window._fmtPct = function (v) {
    return (+v).toFixed(1) + '%';
};

/* ── Dashboard state ─────────────────────────────────────────────────────── */
// Private array tracks ApexCharts instances so they can be destroyed on re-render.
var _dashboardCharts = [];

/* ── renderDashboard ─────────────────────────────────────────────────────── */
/**
 * Render the All Donations multi-widget dashboard into #dashboard-container.
 *
 * @param {Object} data  Parsed chart_data_json from the server when type === 'dashboard'.
 */
window.renderDashboard = function (data) {
    var container = document.getElementById('dashboard-container');
    if (!container) return;

    _dashboardCharts.forEach(function (c) { c.destroy(); });
    _dashboardCharts = [];

    var kpi = data.kpi || {};
    var w = data.widgets || {};
    var trends = kpi.trends || {};

    /* ── Layout helpers ── */
    function secHeader(title) {
        return '<div class="flex items-center gap-3 pt-2">'
            + '<h3 class="text-sm font-semibold text-gray-700 whitespace-nowrap">' + title + '</h3>'
            + '<div class="flex-1 h-px bg-gray-200"></div>'
            + '</div>';
    }

    function wCard(id) {
        var t = (w[id] && w[id].title) ? w[id].title : id.replace(/_/g, ' ');
        return '<div class="bg-white border border-gray-200 rounded-xl p-4">'
            + '<h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">' + t + '</h4>'
            + '<div id="dash-chart-' + id + '" style="min-height:280px;"></div>'
            + '</div>';
    }

    function wRow(ids, cols) {
        var cc = (cols === 2) ? 'grid-cols-1 lg:grid-cols-2' : 'grid-cols-1 lg:grid-cols-3';
        return '<div class="grid ' + cc + ' gap-4">' + ids.map(wCard).join('') + '</div>';
    }

    /* ── Build HTML ── */
    var html = '<div class="space-y-5">';

    /* Section 1: Overview KPIs */
    html += secHeader('Overview');
    html += '<div class="grid grid-cols-2 lg:grid-cols-4 gap-4">';
    [
        { title: 'Total Donations', value: _fmtGBP(kpi.total_amount), trend: trends.total_amount },
        { title: 'Donation Count', value: kpi.total_count, trend: trends.total_count },
        { title: 'Average Donation', value: _fmtGBP(kpi.avg_amount), trend: trends.avg_amount },
        { title: 'Gift Aid Value', value: _fmtGBP(kpi.gift_aid_value), trend: trends.gift_aid_value },
    ].forEach(function (k) {
        var tColor = (k.trend > 0) ? '#10B981' : (k.trend < 0 ? '#EF4444' : '#6B7280');
        var tIcon = (k.trend > 0) ? '&#8593;' : (k.trend < 0 ? '&#8595;' : '&minus;');
        html += '<div class="bg-white rounded-xl p-4 border border-gray-200">';
        html += '<p class="text-xs text-gray-500 mb-1">' + k.title + '</p>';
        html += '<p class="text-xl font-bold text-gray-900 mt-1">' + k.value + '</p>';
        if (k.trend !== null && k.trend !== undefined) {
            html += '<p class="text-xs mt-1" style="color:' + tColor + ';">' + tIcon + ' '
                + Math.abs(k.trend).toFixed(1) + '% '
                + '<span style="color:#9CA3AF;font-weight:400;margin-left:4px;">vs previous period</span></p>';
        }
        html += '</div>';
    });
    html += '</div>';

    /* Section 2: Payment & Revenue */
    html += secHeader('Payment &amp; Revenue');
    html += wRow(['dist_payment', 'trend_payment', 'trend_revenue'], 3);

    /* Section 3: Campaign Performance */
    html += secHeader('Campaign Performance');
    html += wRow(['dist_campaign', 'trend_campaign', 'dist_high_value'], 3);

    /* Section 4: Donor Insights */
    html += secHeader('Donor Insights');
    html += wRow(['top_donors', 'dist_bands', 'trend_count'], 3);

    /* Section 5: Gift Aid & Compliance */
    html += secHeader('Gift Aid &amp; Compliance');
    html += wRow(['dist_gift_aid', 'dist_sla'], 2);

    /* Section 6: Geographic Distribution */
    html += secHeader('Geographic Distribution');
    html += '<div class="bg-white border border-gray-200 rounded-xl p-4">';
    html += '<h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">'
        + ((w['geo_map'] && w['geo_map'].title) || 'Donation Density by Region') + '</h4>';
    html += '<div id="dash-chart-geo_map" style="min-height:380px;"></div>';
    html += '</div>';

    html += '</div>';
    container.innerHTML = html;

    /* ── Instantiate ApexCharts for each widget ── */
    Object.keys(w).forEach(function (id) {
        var widget = w[id];
        var el = document.getElementById('dash-chart-' + id);
        if (!el) return;

        if (widget.type === 'uk_map') {
            if (window.initLeafletMap) window.initLeafletMap('dash-chart-geo_map', widget.data);
            return;
        }

        var options = {};
        var palette = window._palette;

        switch (widget.type) {
            case 'donut':
            case 'pie':
                options = {
                    chart: { type: widget.type, height: 320, fontFamily: window._fontSans, animations: { enabled: true } },
                    series: widget.series, labels: widget.labels,
                    colors: ['#3B82F6', '#10B981', '#F59E0B', '#8B5CF6', '#6366F1', '#EC4899', '#06B6D4', '#F43F5E'],
                    legend: { position: 'bottom', fontSize: '12px' },
                    dataLabels: { enabled: true, formatter: _fmtPct },
                    plotOptions: { pie: { donut: { size: '65%' } } },
                    tooltip: {
                        theme: 'light',
                        y: { formatter: function (v) { return id.includes('count') ? v : _fmtGBP(v); } },
                    },
                };
                break;

            case 'bar_grouped':
                options = {
                    chart: { type: 'bar', height: 320, toolbar: { show: false }, animations: { enabled: true } },
                    series: widget.series,
                    xaxis: { categories: widget.categories, labels: { style: { fontSize: '10px' } } },
                    colors: ['#3B82F6', '#10B981', '#F59E0B', '#8B5CF6', '#6366F1'],
                    plotOptions: { bar: { borderRadius: 4, columnWidth: '55%' } },
                    yaxis: { labels: { formatter: _fmtGBPShort, style: { fontSize: '10px' } } },
                    legend: { position: 'top', horizontalAlign: 'right' },
                };
                break;

            case 'area_single':
                options = {
                    chart: { type: 'area', height: 320, toolbar: { show: false }, animations: { enabled: true } },
                    series: widget.series,
                    xaxis: { categories: widget.categories, labels: { style: { fontSize: '10px' } } },
                    colors: [id === 'trend_count' ? '#8B5CF6' : '#3B82F6'],
                    stroke: { curve: 'smooth', width: 3 },
                    fill: { type: 'gradient', gradient: { shadeIntensity: 1, opacityFrom: 0.45, opacityTo: 0.05 } },
                    yaxis: {
                        labels: {
                            formatter: id === 'trend_count' ? function (v) { return v; } : _fmtGBPShort,
                            style: { fontSize: '10px' },
                        },
                    },
                };
                break;

            case 'bar_column':
                options = {
                    chart: { type: 'bar', height: 320, toolbar: { show: false } },
                    series: widget.series,
                    xaxis: { categories: widget.categories },
                    colors: ['#3B82F6', '#94A3B8'],
                    plotOptions: { bar: { borderRadius: 6, columnWidth: '45%', distributed: true } },
                    yaxis: { labels: { formatter: _fmtGBPShort } },
                    dataLabels: { enabled: true, formatter: _fmtGBPShort },
                };
                break;

            case 'bar_horizontal':
                options = {
                    chart: {
                        type: 'bar',
                        height: Math.max(300, (widget.categories || []).length * 32),
                        toolbar: { show: false },
                    },
                    series: widget.series,
                    plotOptions: { bar: { horizontal: true, borderRadius: 4, barHeight: '60%' } },
                    xaxis: { categories: widget.categories, labels: { formatter: _fmtGBPShort, style: { fontSize: '11px' } } },
                    yaxis: { labels: { style: { fontSize: '11px' } } },
                    colors: ['#3B82F6'],
                    tooltip: { y: { formatter: _fmtGBP } },
                };
                break;

            default:
                return;
        }

        if (options.chart) {
            var chart = new ApexCharts(el, options);
            chart.render();
            _dashboardCharts.push(chart);
        }
    });
};

/* ── initLeafletMap ──────────────────────────────────────────────────────── */
/**
 * Fallback Leaflet map renderer — only registered if a page-level implementation
 * has not already been provided (e.g. the full-page reports.html defines a richer
 * version tuned to the EER GeoJSON property names).
 *
 * @param {string} containerId  ID of the container element.
 * @param {Object} mapData      Object mapping region name → donation total.
 */
if (!window.initLeafletMap) {
    window.initLeafletMap = function (containerId, mapData) {
        if (typeof L === 'undefined') return;
        if (!window.UK_REGIONS_GEOJSON) return;

        var el = document.getElementById(containerId);
        if (!el) return;

        if (window._currentLeafletMap) {
            try { window._currentLeafletMap.remove(); } catch (e) { /* ignore */ }
            window._currentLeafletMap = null;
        }

        var maxVal = Math.max.apply(null, Object.values(mapData).concat([1]));
        var map = L.map(containerId, { zoomControl: true, scrollWheelZoom: false });

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; OpenStreetMap contributors',
        }).addTo(map);

        L.geoJSON(window.UK_REGIONS_GEOJSON, {
            style: function (feature) {
                var regionName = feature.properties && (feature.properties.EER13NM || feature.properties.nuts118nm);
                var value = mapData[regionName] || 0;
                var ratio = maxVal > 0 ? value / maxVal : 0;
                return { fillColor: '#3B82F6', fillOpacity: 0.15 + ratio * 0.75, color: '#fff', weight: 1 };
            },
            onEachFeature: function (feature, layer) {
                var regionName = feature.properties && (feature.properties.EER13NM || feature.properties.nuts118nm);
                var value = mapData[regionName] || 0;
                layer.bindTooltip('<b>' + regionName + '</b><br/>' + _fmtGBP(value), { sticky: true });
            },
        }).addTo(map);

        map.fitBounds(L.geoJSON(window.UK_REGIONS_GEOJSON).getBounds());
        window._currentLeafletMap = map;
    };
}
