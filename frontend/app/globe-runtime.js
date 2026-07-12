/* SIGNAL Cesium globe runtime. Exposes window.SignalGlobe.mount(). */
(function () {
  "use strict";

  const TOKEN_KEY = "SIGNAL_CESIUM_ION_TOKEN";
  const COUNTRY_BORDERS_URL = "https://cdn.jsdelivr.net/gh/johan/world.geo.json@master/countries.geo.json";
  const DETAIL_COUNTRY_BORDERS_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_0_boundary_lines_land.geojson";
  const DETAIL_ADMIN_BORDERS_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_1_states_provinces_lines.geojson";
  const BORDER_TEXTURE_WIDTH = 6144;
  const BORDER_TEXTURE_HEIGHT = 3072;
  const BORDER_SEAM_PAD = 24;
  const DETAIL_ZOOM_HEIGHT = 3600000;
  const BORDER_MIN_ZOOM_DISTANCE = 850000;
  const MAX_RESOLUTION_SCALE = 2.25;
  const MAP_MODES = {
    earth: {
      label: "Earth sphere",
      status: "Earth sphere · Natural Earth imagery · add ion token for terrain",
      tokenStatus: "Earth sphere · Natural Earth imagery · terrain enabled when ion assets are available",
    },
    borders: {
      label: "Abstract borders",
      status: "Abstract borders · high-resolution country boundaries",
      detailStatus: "Abstract borders · country and regional detail active",
      loadingStatus: "Abstract borders · rendering country boundaries",
      detailLoadingStatus: "Abstract borders · loading close-detail boundaries",
      errorStatus: "Abstract borders unavailable · showing black/white globe",
    },
  };
  const RISK_COLORS = {
    low: "#2dd4a7",
    medium: "#f0b03e",
    high: "#f97336",
    critical: "#f4515f",
  };

  function riskLevel(score) {
    return score >= 80 ? "critical" : score >= 65 ? "high" : score >= 45 ? "medium" : "low";
  }

  function riskColor(score) {
    return RISK_COLORS[riskLevel(score)];
  }

  function hexToRgba(hex, alpha) {
    const raw = String(hex || "#38bdf8").replace("#", "");
    const value = raw.length === 3
      ? raw.split("").map((char) => char + char).join("")
      : raw.padEnd(6, "0").slice(0, 6);
    const r = parseInt(value.slice(0, 2), 16);
    const g = parseInt(value.slice(2, 4), 16);
    const b = parseInt(value.slice(4, 6), 16);
    return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
  }

  function normalizeMapMode(mode) {
    return mode === "borders" ? "borders" : "earth";
  }

  function makeTargetMarker(colorCss, selected, monochrome) {
    const size = selected ? 52 : 18;
    const ratio = Math.min(3, Math.max(2, window.devicePixelRatio || 1));
    const canvas = document.createElement("canvas");
    canvas.width = size * ratio;
    canvas.height = size * ratio;
    canvas.style.width = size + "px";
    canvas.style.height = size + "px";

    const ctx = canvas.getContext("2d");
    const cx = size / 2;
    const cy = size / 2;
    ctx.scale(ratio, ratio);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";

    if (!selected) {
      ctx.fillStyle = monochrome ? "rgba(255,255,255,.88)" : hexToRgba(colorCss, 0.82);
      ctx.strokeStyle = monochrome ? "rgba(0,0,0,.92)" : "rgba(245,248,255,.88)";
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.arc(cx, cy, 5.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      return canvas;
    }

    ctx.shadowColor = monochrome ? "rgba(255,255,255,.42)" : hexToRgba(colorCss, 0.5);
    ctx.shadowBlur = 10;
    ctx.strokeStyle = monochrome ? "rgba(255,255,255,.96)" : hexToRgba(colorCss, 0.92);
    ctx.lineWidth = 2.2;
    ctx.beginPath();
    ctx.arc(cx, cy, 15.5, 0, Math.PI * 2);
    ctx.stroke();

    ctx.shadowBlur = 0;
    ctx.strokeStyle = "rgba(245,248,255,.9)";
    ctx.lineWidth = 1.1;
    ctx.beginPath();
    ctx.arc(cx, cy, 22, 0, Math.PI * 2);
    ctx.stroke();

    ctx.strokeStyle = "rgba(245,248,255,.95)";
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.moveTo(cx, 4);
    ctx.lineTo(cx, 13);
    ctx.moveTo(cx, size - 4);
    ctx.lineTo(cx, size - 13);
    ctx.moveTo(4, cy);
    ctx.lineTo(13, cy);
    ctx.moveTo(size - 4, cy);
    ctx.lineTo(size - 13, cy);
    ctx.stroke();

    ctx.fillStyle = monochrome ? "#0a0a0a" : colorCss;
    ctx.beginPath();
    ctx.arc(cx, cy, 4.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "rgba(5,10,18,.92)";
    ctx.lineWidth = 1.1;
    ctx.stroke();
    return canvas;
  }

  function formalMarkerText(loc) {
    const name = (loc.mapLabel || loc.label || "Location").toUpperCase();
    const type = String(loc.sourceType || "Signal node").toUpperCase();
    const risk = "RISK " + (loc.riskScore || "--");
    const coords = Number(loc.latitude).toFixed(4) + ", " + Number(loc.longitude).toFixed(4);
    return [name, type + " / " + risk, coords].join("\n");
  }

  function distanceKm(a, b) {
    const toRad = Math.PI / 180;
    const lat1 = Number(a.latitude) * toRad;
    const lat2 = Number(b.latitude) * toRad;
    const dLat = (Number(b.latitude) - Number(a.latitude)) * toRad;
    const dLon = (Number(b.longitude) - Number(a.longitude)) * toRad;
    const h = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 6371 * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h));
  }

  function getIonToken() {
    const meta = document.querySelector('meta[name="signal-cesium-ion-token"]');
    return (
      window.SIGNAL_CESIUM_ION_TOKEN ||
      window.localStorage.getItem(TOKEN_KEY) ||
      (meta && meta.content) ||
      ""
    ).trim();
  }

  function makeFallback(container, message) {
    container.replaceChildren();
    const box = document.createElement("div");
    box.className = "geo-map-fallback";
    box.innerHTML =
      '<div class="eyebrow">Cesium map unavailable</div>' +
      '<div class="geo-map-fallback-title">Detailed Earth map could not load</div>' +
      '<p>' + message + "</p>";
    container.appendChild(box);
    return {
      setLocations() {},
      setMapMode() {},
      focusLocation() {},
      dispose() { container.replaceChildren(); },
    };
  }

  async function createViewer(container, options) {
    if (!window.Cesium) {
      return makeFallback(container, "CesiumJS was not found. Serve this page over http and allow the Cesium CDN scripts to load.");
    }

    const Cesium = window.Cesium;
    const token = getIonToken();
    const state = {
      locations: options.locations || [],
      selectedId: options.selectedId || null,
      mapMode: normalizeMapMode(options.mapMode),
      onSelect: options.onSelect || null,
      viewer: null,
      naturalLayer: null,
      borderLayer: null,
      borderPromise: null,
      detailLayer: null,
      detailPromise: null,
      detailVisible: false,
      detailPreloadTimer: null,
      textureUrls: [],
      markerById: new Map(),
      buildingTileset: null,
      removeCameraWatcher: null,
      destroyed: false,
    };

    if (token) Cesium.Ion.defaultAccessToken = token;

    container.replaceChildren();
    const viewerNode = document.createElement("div");
    viewerNode.className = "geo-cesium-viewer";
    container.appendChild(viewerNode);

    const naturalEarthProvider = await Cesium.TileMapServiceImageryProvider.fromUrl(
      Cesium.buildModuleUrl("Assets/Textures/NaturalEarthII")
    );

    const viewerOptions = {
      animation: false,
      baseLayerPicker: false,
      baseLayer: new Cesium.ImageryLayer(naturalEarthProvider),
      fullscreenButton: false,
      geocoder: false,
      homeButton: false,
      infoBox: false,
      navigationHelpButton: false,
      sceneMode: Cesium.SceneMode.SCENE3D,
      sceneModePicker: false,
      selectionIndicator: false,
      timeline: false,
      vrButton: false,
      shouldAnimate: true,
      skyBox: false,
      skyAtmosphere: false,
    };

    if (token && Cesium.Terrain && Cesium.Terrain.fromWorldTerrain) {
      viewerOptions.terrain = Cesium.Terrain.fromWorldTerrain();
    }

    const viewer = new Cesium.Viewer(viewerNode, viewerOptions);
    state.viewer = viewer;
    state.naturalLayer = viewer.imageryLayers.length ? viewer.imageryLayers.get(0) : null;
    viewer.resolutionScale = Math.min(MAX_RESOLUTION_SCALE, Math.max(1.5, window.devicePixelRatio || 1));
    if ("useBrowserRecommendedResolution" in viewer) viewer.useBrowserRecommendedResolution = false;
    if ("msaaSamples" in viewer.scene) viewer.scene.msaaSamples = 4;
    if (viewer.scene.postProcessStages && viewer.scene.postProcessStages.fxaa) {
      viewer.scene.postProcessStages.fxaa.enabled = true;
    }
    viewer.scene.globe.depthTestAgainstTerrain = true;
    viewer.scene.globe.enableLighting = false;
    viewer.scene.globe.showGroundAtmosphere = false;
    viewer.scene.globe.maximumScreenSpaceError = 1.25;
    viewer.scene.fog.enabled = false;
    viewer.scene.backgroundColor = Cesium.Color.fromCssColorString(
      getComputedStyle(document.documentElement).getPropertyValue("--surface").trim() || "#11161f"
    );
    if (viewer.scene.skyBox) viewer.scene.skyBox.show = false;
    if (viewer.scene.skyAtmosphere) viewer.scene.skyAtmosphere.show = false;
    if (viewer.scene.sun) viewer.scene.sun.show = false;
    if (viewer.scene.moon) viewer.scene.moon.show = false;
    viewer.scene.screenSpaceCameraController.minimumZoomDistance = 120;
    viewer.scene.screenSpaceCameraController.maximumZoomDistance = 42000000;

    const credit = document.createElement("div");
    credit.className = "geo-cesium-status";
    credit.textContent = token ? MAP_MODES.earth.tokenStatus : MAP_MODES.earth.status;
    container.appendChild(credit);

    if (token && Cesium.createOsmBuildingsAsync) {
      Cesium.createOsmBuildingsAsync()
        .then((tileset) => {
          if (state.destroyed || !state.viewer) return;
          state.buildingTileset = tileset;
          tileset.show = state.mapMode === "earth";
          state.viewer.scene.primitives.add(tileset);
        })
        .catch((err) => {
          console.warn("[SIGNAL] Cesium OSM buildings unavailable:", err);
          if (state.mapMode === "earth") {
            credit.textContent = "Earth sphere · terrain active · OSM buildings unavailable for this token";
          }
        });
    }

    function surfaceSceneColor() {
      return Cesium.Color.fromCssColorString(
        getComputedStyle(document.documentElement).getPropertyValue("--surface").trim() || "#11161f"
      );
    }

    function projectBorderPoint(point) {
      return {
        x: ((Number(point[0]) + 180) / 360) * BORDER_TEXTURE_WIDTH,
        y: ((90 - Number(point[1])) / 180) * BORDER_TEXTURE_HEIGHT,
      };
    }

    function drawBorderRing(ctx, ring) {
      if (!Array.isArray(ring) || ring.length < 2) return;
      let drawing = false;
      let previous = null;

      ring.forEach((point) => {
        if (!Array.isArray(point) || point.length < 2) return;
        const projected = projectBorderPoint(point);
        if (!Number.isFinite(projected.x) || !Number.isFinite(projected.y)) return;
        if (projected.x < BORDER_SEAM_PAD || projected.x > BORDER_TEXTURE_WIDTH - BORDER_SEAM_PAD) {
          if (drawing) ctx.stroke();
          drawing = false;
          previous = null;
          return;
        }
        if (!previous || Math.abs(projected.x - previous.x) > BORDER_TEXTURE_WIDTH / 2) {
          if (drawing) ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(projected.x, projected.y);
          drawing = true;
        } else {
          ctx.lineTo(projected.x, projected.y);
        }
        previous = projected;
      });

      if (drawing) ctx.stroke();
    }

    function drawBoundaryGeometry(ctx, geometry) {
      if (!geometry) return;
      switch (geometry.type) {
        case "LineString":
          if (!Array.isArray(geometry.coordinates)) break;
          drawBorderRing(ctx, geometry.coordinates);
          break;
        case "MultiLineString":
          if (!Array.isArray(geometry.coordinates)) break;
          geometry.coordinates.forEach((line) => drawBorderRing(ctx, line));
          break;
        case "Polygon":
          if (!Array.isArray(geometry.coordinates)) break;
          geometry.coordinates.forEach((ring) => drawBorderRing(ctx, ring));
          break;
        case "MultiPolygon":
          if (!Array.isArray(geometry.coordinates)) break;
          geometry.coordinates.forEach((polygon) => {
            polygon.forEach((ring) => drawBorderRing(ctx, ring));
          });
          break;
        case "GeometryCollection":
          (geometry.geometries || []).forEach((child) => drawBoundaryGeometry(ctx, child));
          break;
        default:
          break;
      }
    }

    function drawBoundaryFeature(ctx, feature) {
      if (feature && feature.geometry) drawBoundaryGeometry(ctx, feature.geometry);
    }

    function drawBoundaryLayer(ctx, geojson, strokes) {
      const features = geojson && Array.isArray(geojson.features) ? geojson.features : [];
      strokes.forEach((stroke) => {
        ctx.strokeStyle = stroke.color;
        ctx.lineWidth = stroke.width;
        features.forEach((feature) => drawBoundaryFeature(ctx, feature));
      });
    }

    function createTextureCanvas() {
      const canvas = document.createElement("canvas");
      canvas.width = BORDER_TEXTURE_WIDTH;
      canvas.height = BORDER_TEXTURE_HEIGHT;

      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, BORDER_TEXTURE_WIDTH, BORDER_TEXTURE_HEIGHT);
      ctx.imageSmoothingEnabled = true;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      return { canvas, ctx };
    }

    function createBorderTexture(geojson) {
      const texture = createTextureCanvas();
      drawBoundaryLayer(texture.ctx, geojson, [
        { color: "rgba(255,255,255,.26)", width: 5.2 },
        { color: "rgba(255,255,255,.92)", width: 2.15 },
      ]);
      return texture.canvas;
    }

    function createDetailBorderTexture(layers) {
      const texture = createTextureCanvas();
      const countryLayers = layers.filter((layer) => layer.kind === "country");
      const adminLayers = layers.filter((layer) => layer.kind === "admin");

      adminLayers.forEach((layer) => drawBoundaryLayer(texture.ctx, layer.geojson, [
        { color: "rgba(255,255,255,.18)", width: 2.1 },
      ]));
      countryLayers.forEach((layer) => drawBoundaryLayer(texture.ctx, layer.geojson, [
        { color: "rgba(255,255,255,.32)", width: 4.2 },
        { color: "rgba(255,255,255,.96)", width: 1.55 },
      ]));
      adminLayers.forEach((layer) => drawBoundaryLayer(texture.ctx, layer.geojson, [
        { color: "rgba(255,255,255,.52)", width: 0.95 },
      ]));

      return texture.canvas;
    }

    function createTextureUrl(canvas) {
      if (!canvas.toBlob) return Promise.resolve(canvas.toDataURL("image/png"));
      return new Promise((resolve) => {
        canvas.toBlob((blob) => {
          if (!blob) {
            resolve(canvas.toDataURL("image/png"));
            return;
          }
          const url = URL.createObjectURL(blob);
          if (state.destroyed) {
            URL.revokeObjectURL(url);
          } else {
            state.textureUrls.push(url);
          }
          resolve(url);
        }, "image/png");
      });
    }

    function createSingleTileProvider(url) {
      if (Cesium.SingleTileImageryProvider && Cesium.SingleTileImageryProvider.fromUrl) {
        return Cesium.SingleTileImageryProvider.fromUrl(url, {
          rectangle: Cesium.Rectangle.MAX_VALUE,
        });
      }
      return new Cesium.SingleTileImageryProvider({
        url,
        rectangle: Cesium.Rectangle.MAX_VALUE,
      });
    }

    function loadCountryBorders() {
      if (state.borderLayer) {
        state.borderLayer.show = state.mapMode === "borders";
        return Promise.resolve(state.borderLayer);
      }
      if (state.borderPromise) return state.borderPromise;

      state.borderPromise = fetch(COUNTRY_BORDERS_URL)
        .then((response) => {
          if (!response.ok) throw new Error("Country border request failed: " + response.status);
          return response.json();
        })
        .then((geojson) => {
          if (state.destroyed || !state.viewer) return null;
          const textureCanvas = createBorderTexture(geojson);
          return createTextureUrl(textureCanvas).then((textureUrl) => Promise.resolve(createSingleTileProvider(textureUrl))).then((provider) => {
            if (state.destroyed || !state.viewer) return null;
            const layerOptions = { alpha: 1 };
            if (Cesium.TextureMinificationFilter && Cesium.TextureMagnificationFilter) {
              layerOptions.minificationFilter = Cesium.TextureMinificationFilter.LINEAR;
              layerOptions.magnificationFilter = Cesium.TextureMagnificationFilter.LINEAR;
            }
            const layer = new Cesium.ImageryLayer(provider, layerOptions);
            layer.show = state.mapMode === "borders";
            layer.alpha = state.detailVisible ? 0.12 : 1;
            viewer.imageryLayers.add(layer);
            state.borderLayer = layer;
            if (state.mapMode === "borders") {
              credit.textContent = state.detailVisible && state.detailLayer
                ? MAP_MODES.borders.detailStatus
                : MAP_MODES.borders.status;
              scheduleDetailPreload();
            }
            return layer;
          });
        }).catch((err) => {
          console.warn("[SIGNAL] Country borders unavailable:", err);
          state.borderPromise = null;
          if (state.mapMode === "borders") credit.textContent = MAP_MODES.borders.errorStatus;
          return null;
        });
      return state.borderPromise;
    }

    function loadCloseDetailBorders(background) {
      if (state.detailLayer) {
        state.detailLayer.show = state.mapMode === "borders" && state.detailVisible;
        return Promise.resolve(state.detailLayer);
      }
      if (state.detailPromise) {
        if (!background && state.mapMode === "borders") credit.textContent = MAP_MODES.borders.detailLoadingStatus;
        return state.detailPromise;
      }

      if (!background && state.mapMode === "borders") credit.textContent = MAP_MODES.borders.detailLoadingStatus;
      const fetchLayer = (url, kind) => fetch(url)
        .then((response) => {
          if (!response.ok) throw new Error("Close-detail border request failed: " + response.status);
          return response.json();
        }).then((geojson) => ({ geojson, kind }));

      state.detailPromise = Promise.allSettled([
        fetchLayer(DETAIL_COUNTRY_BORDERS_URL, "country"),
        fetchLayer(DETAIL_ADMIN_BORDERS_URL, "admin"),
      ]).then((results) => {
          if (state.destroyed || !state.viewer) return null;
          const layers = [];
          results.forEach((result) => {
            if (result.status !== "fulfilled") {
              console.warn("[SIGNAL] A close-detail border layer was unavailable:", result.reason);
              return;
            }
            layers.push(result.value);
          });
          if (!layers.length) throw new Error("No close-detail border layers loaded.");
          const textureCanvas = createDetailBorderTexture(layers);
          return createTextureUrl(textureCanvas).then((textureUrl) => Promise.resolve(createSingleTileProvider(textureUrl))).then((provider) => {
            if (state.destroyed || !state.viewer) return null;
            const layerOptions = { alpha: 1 };
            if (Cesium.TextureMinificationFilter && Cesium.TextureMagnificationFilter) {
              layerOptions.minificationFilter = Cesium.TextureMinificationFilter.LINEAR;
              layerOptions.magnificationFilter = Cesium.TextureMagnificationFilter.LINEAR;
            }
            const layer = new Cesium.ImageryLayer(provider, layerOptions);
            layer.show = state.mapMode === "borders" && state.detailVisible;
            state.detailLayer = layer;
            viewer.imageryLayers.add(layer);
            if (state.mapMode === "borders") {
              credit.textContent = state.detailVisible ? MAP_MODES.borders.detailStatus : MAP_MODES.borders.status;
            }
            return layer;
          });
      }).catch((err) => {
          console.warn("[SIGNAL] Close-detail country borders unavailable:", err);
          state.detailPromise = null;
          if (state.mapMode === "borders") credit.textContent = MAP_MODES.borders.status;
          return null;
        });
      return state.detailPromise;
    }

    function scheduleDetailPreload() {
      if (state.detailLayer || state.detailPromise || state.detailPreloadTimer) return;
      state.detailPreloadTimer = window.setTimeout(() => {
        state.detailPreloadTimer = null;
        if (!state.destroyed && state.mapMode === "borders") loadCloseDetailBorders(true);
      }, 350);
    }

    function cameraHeight() {
      const cartographic = viewer.camera.positionCartographic;
      return cartographic && Number.isFinite(cartographic.height) ? cartographic.height : Infinity;
    }

    function updateDetailVisibility() {
      if (state.destroyed || !viewer) return;
      const shouldShowDetail = state.mapMode === "borders" && cameraHeight() <= DETAIL_ZOOM_HEIGHT;
      if (state.detailVisible === shouldShowDetail) {
        if (shouldShowDetail && !state.detailLayer && !state.detailPromise) loadCloseDetailBorders(false);
        return;
      }

      state.detailVisible = shouldShowDetail;
      if (state.detailLayer) state.detailLayer.show = shouldShowDetail;
      if (state.borderLayer) state.borderLayer.alpha = shouldShowDetail ? 0.12 : 1;
      if (state.mapMode !== "borders") return;

      if (shouldShowDetail) {
        if (state.detailLayer) {
          credit.textContent = MAP_MODES.borders.detailStatus;
        } else {
          loadCloseDetailBorders(false);
        }
      } else {
        credit.textContent = state.borderLayer ? MAP_MODES.borders.status : MAP_MODES.borders.loadingStatus;
      }
    }

    function applyMapMode() {
      const abstractMode = state.mapMode === "borders";
      viewerNode.dataset.mapMode = state.mapMode;
      if (state.naturalLayer) state.naturalLayer.show = !abstractMode;
      if (state.borderLayer) state.borderLayer.show = abstractMode;
      if (state.borderLayer) state.borderLayer.alpha = abstractMode && state.detailVisible ? 0.12 : 1;
      if (state.detailLayer) state.detailLayer.show = abstractMode && state.detailVisible;
      if (state.buildingTileset) state.buildingTileset.show = !abstractMode;
      viewer.scene.screenSpaceCameraController.minimumZoomDistance = abstractMode ? BORDER_MIN_ZOOM_DISTANCE : 120;

      viewer.scene.backgroundColor = abstractMode ? Cesium.Color.BLACK : surfaceSceneColor();
      viewer.scene.globe.baseColor = abstractMode ? Cesium.Color.BLACK : surfaceSceneColor();
      credit.textContent = abstractMode
        ? (state.detailVisible && state.detailLayer
          ? MAP_MODES.borders.detailStatus
          : (state.borderLayer ? MAP_MODES.borders.status : MAP_MODES.borders.loadingStatus))
        : (token ? MAP_MODES.earth.tokenStatus : MAP_MODES.earth.status);

      if (abstractMode) {
        loadCountryBorders().then(() => {
          if (!state.destroyed && state.mapMode === "borders") scheduleDetailPreload();
        });
      }
      updateDetailVisibility();
    }

    function selectedLocation() {
      return state.locations.find((loc) => loc.id === state.selectedId) || state.locations[0];
    }

    function markerDescription(loc) {
      return [
        "<strong>" + loc.label + "</strong>",
        "<br/>" + loc.address,
        "<br/>Risk score: " + loc.riskScore,
        "<br/>Signal type: " + loc.sourceType,
      ].join("");
    }

    function rebuildMarkers() {
      state.markerById.forEach((entity) => viewer.entities.remove(entity));
      state.markerById.clear();

      const selected = selectedLocation();
      const abstractMode = state.mapMode === "borders";
      state.locations.forEach((loc) => {
        const selectedMarker = selected && loc.id === selected.id;
        if (selected && !selectedMarker && distanceKm(loc, selected) < 500) return;
        const colorCss = abstractMode ? "#ffffff" : riskColor(loc.riskScore || 40);
        const color = Cesium.Color.fromCssColorString(colorCss);
        const entity = viewer.entities.add({
          id: "geo-" + loc.id,
          name: loc.label,
          position: Cesium.Cartesian3.fromDegrees(loc.longitude, loc.latitude, 0),
          billboard: {
            image: makeTargetMarker(colorCss, selectedMarker, abstractMode),
            horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
            verticalOrigin: Cesium.VerticalOrigin.CENTER,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
            disableDepthTestDistance: 18000000,
            scale: 1,
            pixelOffset: new Cesium.Cartesian2(0, 0),
          },
          ellipse: selectedMarker ? {
            semiMajorAxis: 12000,
            semiMinorAxis: 12000,
            material: color.withAlpha(abstractMode ? 0.06 : 0.12),
            outline: true,
            outlineColor: color.withAlpha(abstractMode ? 0.92 : 0.72),
            height: 0,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          } : undefined,
          label: selectedMarker ? {
            text: formalMarkerText(loc),
            font: "600 12px IBM Plex Mono, ui-monospace, SFMono-Regular, Menlo, monospace",
            fillColor: Cesium.Color.WHITE,
            outlineColor: Cesium.Color.BLACK.withAlpha(0.86),
            outlineWidth: 2,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            showBackground: true,
            backgroundColor: Cesium.Color.fromCssColorString("#07101a").withAlpha(0.84),
            backgroundPadding: new Cesium.Cartesian2(10, 7),
            horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
            pixelOffset: new Cesium.Cartesian2(0, -42),
            verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
            disableDepthTestDistance: 18000000,
          } : undefined,
          description: markerDescription(loc),
          properties: {
            signalLocationId: loc.id,
          },
        });
        state.markerById.set(loc.id, entity);
      });

      focusLocation(selected, false);
    }

    function focusLocation(loc, closeView) {
      if (!loc || !viewer || state.destroyed) return;
      const highAltitude = 7800000;
      const closeAltitude = token ? 1450000 : 2200000;
      viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(loc.longitude, loc.latitude, closeView ? closeAltitude : highAltitude),
        orientation: {
          heading: Cesium.Math.toRadians(0),
          pitch: Cesium.Math.toRadians(closeView ? -82 : -90),
          roll: 0,
        },
        duration: 0.9,
      });
    }

    const clickHandler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
    clickHandler.setInputAction((movement) => {
      const picked = viewer.scene.pick(movement.position);
      if (!Cesium.defined(picked) || !picked.id || !picked.id.properties) return;
      const prop = picked.id.properties.signalLocationId;
      const id = prop && typeof prop.getValue === "function" ? prop.getValue() : null;
      if (id && state.onSelect) state.onSelect(id);
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
    state.removeCameraWatcher = viewer.camera.moveEnd.addEventListener(updateDetailVisibility);

    applyMapMode();
    rebuildMarkers();

    return {
      setLocations(locations, selectedId) {
        state.locations = locations || [];
        state.selectedId = selectedId || null;
        rebuildMarkers();
      },
      focusLocation(location) {
        focusLocation(location, true);
      },
      setMapMode(mapMode) {
        const nextMode = normalizeMapMode(mapMode);
        if (state.mapMode === nextMode) return;
        state.mapMode = nextMode;
        applyMapMode();
        rebuildMarkers();
      },
      dispose() {
        state.destroyed = true;
        if (state.detailPreloadTimer) window.clearTimeout(state.detailPreloadTimer);
        state.textureUrls.forEach((url) => URL.revokeObjectURL(url));
        if (state.removeCameraWatcher) state.removeCameraWatcher();
        clickHandler.destroy();
        if (viewer && !viewer.isDestroyed()) viewer.destroy();
        container.replaceChildren();
      },
    };
  }

  window.SignalGlobe = {
    mount(container, options) {
      let disposed = false;
      const pendingOptions = Object.assign({}, options || {});
      let runtime = makeFallback(container, "Initializing CesiumJS...");
      createViewer(container, pendingOptions).then((next) => {
        if (disposed) {
          next.dispose();
          return;
        }
        runtime = next;
        runtime.setLocations(pendingOptions.locations, pendingOptions.selectedId);
        runtime.setMapMode(pendingOptions.mapMode);
      }).catch((err) => {
        console.error("[SIGNAL] Cesium globe failed:", err);
        runtime.dispose();
        runtime = makeFallback(container, err && err.message ? err.message : "Unknown Cesium runtime error.");
      });

      return {
        setLocations(locations, selectedId) {
          pendingOptions.locations = locations || [];
          pendingOptions.selectedId = selectedId || null;
          runtime.setLocations(locations, selectedId);
        },
        setMapMode(mapMode) {
          pendingOptions.mapMode = normalizeMapMode(mapMode);
          runtime.setMapMode(mapMode);
        },
        focusLocation(location) {
          runtime.focusLocation(location);
        },
        dispose() {
          disposed = true;
          runtime.dispose();
        },
      };
    },
    tokenKey: TOKEN_KEY,
    hasToken: () => !!getIonToken(),
  };

  window.dispatchEvent(new CustomEvent("signal:globe-ready"));
})();
