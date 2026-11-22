# Estandares y conformidad

Este documento fija que norma rige cada pieza del puente, la seccion exacta que
se leyo y donde se comprueba en el codigo. Regla dura: una cita sin seccion es
una mencion, no una cita.

La matriz dice que clausulas se implementan y con que evidencia. No dice
"cumple ISO" ni "cumple RFC" a secas.

---

## Matriz de normas

| Norma | Seccion | Que rige | Donde se comprueba |
|---|---|---|---|
| ISO/IEC 13818-1 \| ITU-T H.222.0 | §2.4.3, §2.4.3.3-.6, §2.4.4.9, Tablas 2-33/2-34, Anexo A | MPEG-TS: paquetes, PCR/PTS/DTS, PMT, CRC_32 | `src/ts_splice.py`, `src/ts_mux.py`; `tests/test_ts_splice.py`, `tests/test_ts_mux.py` |
| RFC 8216 (HLS) | §4.3.2.1, §4.3.2.4, §4.3.3.2 | EXTINF, EXT-X-KEY, MEDIA-SEQUENCE del HLS que se CONSUME | `src/relay.py`; `tests/test_regresiones.py` |
| RFC 9110 (HTTP) | §15.5.4 | 403 Forbidden para recurso conocido y no servible | `src/api.py` |
| XMLTV DTD | `tv`, `channel`, `programme` | Estructura minima de la guia | `src/epg.py`; `tests/test_epg.py` |
| M3U extendido | de facto | Lista de canales publicada | `src/playlist.py` |

Las citas de ISO se verificaron contra el texto de la ITU que vive en el CT:

```
/home/server/tmp_lvb/norma/h222.pdf
/home/server/tmp_lvb/norma/h222.txt
```

---

## ISO/IEC 13818-1 | ITU-T H.222.0 (06/2012) — MPEG-TS

Es la misma norma: ISO/IEC 13818-1 y la recomendacion ITU-T H.222.0 se publican
twin. El texto completo se leyo en el `.txt` local.

### Clausulas fijadas

```
§2.4.3    Transport Stream. La propia recomendacion lo cita como
          "multi-program Transport Stream as described in 2.4.3".
§2.4.3.3  Semantic definition of fields in transport stream packet layer.
          continuity_counter en la Tabla 2-4.
§2.4.3.4  Adaptation field + Tabla 2-6. Sintaxis de adaptation_field_length,
          PCR_flag, OPCR_flag, splicing_point_flag y PCR.
Tabla 2-5 Adaptation field control: 01 = payload, 10 = adaptation only,
          11 = adaptation + payload.
§2.4.3.5  Semantic definition of fields in adaptation field.
          adaptation_field_length 0..182 con '11'; dos semanticas del
          discontinuity_indicator.
§2.4.3.6  PES packets. La introduccion remite a esta clausula.
§2.4.4.9  section_length: incluye el CRC, maximo 1021.
Tabla 2-33 TS_program_map_section(): table_id, longitudes y bucles de ES.
Tabla 2-34 stream_type 0x0F = ISO/IEC 13818-7 Audio con ADTS.
Anexo A   CRC_32: polinomio 0x04C11DB7; se comprueba por autocontrol.
```

### Que implementa el codigo

- `src/ts_splice.py` reescribe PCR, PTS y DTS al cambiar de fuente. No toca el
  `continuity_counter`; la norma lo permite cuando se marca discontinuidad
  (§2.4.3.3 y §2.4.3.5).
- `src/ts_mux.py` arma el PMT con la Tabla 2-33, calcula `section_length` como
  §2.4.4.9 y usa el CRC del Anexo A. El CRC de biblioteca no sirve: se comprueba
  con el autocontrol de la norma.

### Fuera de alcance

No se certifica la norma completa. Solo se implementan las clausulas que el
puente toca: paquetes TS, adaptation field, PMT y CRC. No se generan secciones
que el proyecto no produce (SDT, EIT, TDT, tablas privadas).

---

## RFC 8216 — HTTP Live Streaming (HLS)

Texto oficial: `https://www.rfc-editor.org/rfc/rfc8216.txt`.

El puente CONSUME HLS de Pluto; no lo genera. Estas clausulas rigen el parser
que lee el master y las playlists de origen.

### §4.3.2.1 — EXTINF

Verbatim:

> The EXTINF tag specifies the duration of a Media Segment. It applies only to
> the next Media Segment. This tag is REQUIRED for each Media Segment.

El parser del rele lee la duracion de cada segmento de origen. No la reescribe:
la usa para saber cuando el colchon tiene material.

### §4.3.2.4 — EXT-X-KEY

Verbatim:

> The EXT-X-KEY tag specifies how to decrypt Media Segments. It applies to
> every Media Segment and to every Media Initialization Section declared by an
> EXT-X-MAP tag that appears between it and the next EXT-X-KEY tag in the
> Playlist file with the same KEYFORMAT attribute (or the end of the Playlist
> file).

El parser lee `METHOD=AES-128`, `URI` e `IV`; el rele descifra con OpenSSL. La
llave y el IV no se publican en el M3U.

### §4.3.3.2 — EXT-X-MEDIA-SEQUENCE

Verbatim:

> The EXT-X-MEDIA-SEQUENCE tag indicates the Media Sequence Number of the first
> Media Segment that appears in a Playlist file. [...] The EXT-X-MEDIA-SEQUENCE
> tag MUST appear before the first Media Segment in the Playlist file.

El parser usa la secuencia para no repetir ni saltar segmentos cuando el CDN
reordena la lista.

### Fuera de alcance

El puente no genera HLS. Entrega MPEG-TS continuo por `/relay/<id>`. No se citan
las clausulas de master playlist (`EXT-X-STREAM-INF`) ni de variantes de bitrate
porque el proyecto no las produce.

---

## RFC 9110 — HTTP Semantics

Texto oficial: `https://www.rfc-editor.org/rfc/rfc9110.txt`.

### §15.5.4 — 403 Forbidden

Verbatim:

> The 403 (Forbidden) status code indicates that the server understood the
> request but refuses to fulfill it.

El API devuelve 403 para un canal conocido pero deshabilitado o con DRM. Un id
desconocido devuelve 404. La diferencia es deliberada: 403 dice "lo conozco y no
lo sirvo"; 404 dice "no lo conozco".

---

## XMLTV DTD

Fuente: `https://raw.githubusercontent.com/XMLTV/xmltv/master/xmltv.dtd`.

Declaraciones verificadas:

```
<!ELEMENT tv (channel*, programme*)>
<!ELEMENT channel (display-name+, icon*, url*)>
<!ATTLIST channel id CDATA #REQUIRED>
<!ELEMENT programme (title+, sub-title*, desc*, ...)>
<!ATTLIST programme start CDATA #REQUIRED>
<!ATTLIST programme stop CDATA #IMPLIED>
<!ATTLIST programme channel CDATA #REQUIRED>
```

`channel` exige `id` y al menos un `display-name`; `programme` exige `start` y
`channel`, y `stop` es opcional. `epg.validar_xmltv()` rechaza una guia que no
cumpla antes de publicarla.

---

## M3U extendido (lista publicada)

El M3U que se publica para Jellyfin es un **M3U extendido**: `#EXTM3U`,
`#EXTINF` con atributos `tvg-id`, `tvg-logo`, `group-title` y la URL del canal.
Es un formato de facto, no un RFC. No se cita RFC 8216 para esta lista porque
RFC 8216 describe playlists HLS, no listas de canales IPTV. El tipo de medio
`audio/mpegurl` es la convencion que usan los clientes; no se presenta como
obligacion de una norma.

---

## Normas que se citan pero NO se imponen

- **JWT de Pluto.** El puente usa el token de sesion que emite Pluto, pero no
  implementa ni valida la firma JWT. No se cita RFC 7519 como conformidad.
- **AES-128-CBC.** El descifrado lo hace OpenSSL. El proyecto no implementa el
  cifrador; no se cita FIPS 197 como si fuera codigo propio.
- **Codecs H.264/AAC.** El puente no transcodifica en el camino de relay:
  copia los segmentos del origen. No se cita una norma de codec como
  conformidad de decodificador porque no se leyo su texto.

---

## Como se vuelve a verificar

```
ISO:    grep sobre /home/server/tmp_lvb/norma/h222.txt
RFC:    curl -s https://www.rfc-editor.org/rfc/rfc8216.txt
        curl -s https://www.rfc-editor.org/rfc/rfc9110.txt
XMLTV:  curl -s https://raw.githubusercontent.com/XMLTV/xmltv/master/xmltv.dtd
Pruebas: cd /home/server/jellyfin-livetv-bridge && \
         sudo -u server .venv/bin/python -m pytest -q
```

Una cita nueva se agrega aqui con su seccion y su evidencia. Si no se leyo el
texto, se marca MENCION y no se usa para afirmar conformidad.
