# Estandares y conformidad

Este documento fija que norma rige cada pieza del puente, la seccion exacta que
se leyo y donde se comprueba en el codigo. Regla dura: **una cita sin seccion es
una mencion, no una cita.** Si no se leyo el texto, no se inventa el numero.

La matriz no dice "cumple ISO" ni "cumple RFC" a secas. Dice que clausulas se
implementan, con que evidencia y que queda fuera de alcance.

---

## Matriz de normas

| Norma | Seccion | Que rige | Donde se comprueba |
|---|---|---|---|
| ISO/IEC 13818-1 \| ITU-T H.222.0 (06/2012) | §2.4.3, §2.4.3.3-.6, §2.4.4.9, Tablas 2-33/2-34, Anexo A | MPEG-TS: paquetes, PCR/PTS/DTS, PMT, CRC_32 | `src/ts_splice.py`, `src/ts_mux.py`; `tests/test_ts_splice.py`, `tests/test_ts_mux.py` |
| RFC 8216 (HLS) | §4, §4.3.2.1, §4.3.3.1, §4.3.3.2 | Tipo de medio, EXTINF, TARGETDURATION, MEDIA-SEQUENCE | `src/api.py`, `src/relay.py`; `tests/test_playlist.py` |
| RFC 9110 (HTTP) | §15.5.4 | 403 Forbidden para recurso conocido y no servible | `src/api.py`; `tests/test_api.py` |
| XMLTV DTD | `tv`, `channel`, `programme` | Estructura minima de la guia | `src/epg.py`; `tests/test_epg.py` |

Las citas de ISO se verificaron contra el texto de la ITU que vive en el CT:

```
/home/server/tmp_lvb/norma/h222.pdf   PDF de la ITU (1.7 MB, 228 paginas)
/home/server/tmp_lvb/norma/h222.txt   el mismo texto, buscable
```

---

## ISO/IEC 13818-1 | ITU-T H.222.0 (06/2012) — MPEG-TS

Es la misma norma: ISO/IEC 13818-1 y la recomendacion ITU-T H.222.0 se publican
twin. El texto completo se leyo en el `.txt` local.

### Clausulas fijadas

```
§2.4.3    Transport Stream. La propia recomendacion lo cita como
          "single or multi-program Transport Stream as in 2.4.3".
§2.4.3.3  Semantic definition of fields in transport stream packet layer.
          continuity_counter (4 bits, uimsbf; Tabla 2-4).
§2.4.3.4  Adaptation field + Tabla 2-6. Sintaxis de adaptation_field_length,
          discontinuity_indicator, PCR, OPCR, splicing_point.
Tabla 2-5 Adaptation field control: 01 = payload only, 10 = adaptation only,
          11 = adaptation + payload.
§2.4.3.5  Semantic definition of fields in adaptation field.
          adaptation_field_length "shall be 183" con '10'; dos semanticas del
          discontinuity_indicator.
§2.4.3.6  PES packets. Citado por la introduccion de la recomendacion.
§2.4.4.9  section_length: 12 bits, los dos primeros en '00', cuenta lo que
          sigue INCLUIDO el CRC, maximo 1021.
Tabla 2-33 TS_program_map_section(): table_id 0x02, section_syntax_indicator,
          longitudes y bucles de ES.
Tabla 2-34 stream_type 0x0F = ISO/IEC 13818-7 Audio con ADTS.
Anexo A   CRC_32: polinomio 0x04C11DB7, registros iniciales todos 1, MSB
          primero, sin reflejar. Autocontrol: la seccion con su CRC da 0.
```

### Que implementa el codigo

- `src/ts_splice.py`: desplaza PCR/PTS/DTS al cambiar de fuente. No reescribe el
  `continuity_counter`, y eso es correcto: la norma lo permite cuando el
  `discontinuity_indicator` esta puesto (§2.4.3.3 y §2.4.3.5).
- `src/ts_mux.py`: arma el PMT con la sintaxis de la Tabla 2-33, calcula
  `section_length` como manda §2.4.4.9 y usa el CRC_32 del Anexo A. El CRC de
  biblioteca NO sirve: se comprueba con el autocontrol de la norma.

### Fuera de alcance

No se certifica la norma completa. Solo se implementan las clausulas que el
puente toca: paquetes TS, adaptation field, PMT y CRC. No se implementan
secciones que el proyecto no genera (SDT, EIT, TDT, tablas privadas).

---

## RFC 8216 — HTTP Live Streaming (HLS)

Texto oficial: `https://www.rfc-editor.org/rfc/rfc8216.txt`.

### §4 — Playlists

Verbatim:

> Each Playlist file MUST be identifiable either by the path component of its
> URI or by HTTP Content-Type. In the first case, the path MUST end with .m3u8
> or .m3u. In the second case, the HTTP Content-Type MUST be
> 'application/vnd.apple.mpegurl' or 'audio/mpegurl'. Clients SHOULD refuse to
> parse Playlists that are not so identified.

Consecuencia practica: `audio/x-mpegurl` no es ninguno de los dos y no cumple.
El proyecto usa `audio/mpegurl` para el M3U y `application/vnd.apple.mpegurl`
para el indice HLS.

### §4.3.2.1 — EXTINF

Verbatim:

> The EXTINF tag specifies the duration of a Media Segment. It applies only to
> the next Media Segment. This tag is REQUIRED for each Media Segment.

El constructor de la bifurcacion HLS emite un `#EXTINF` por cada segmento.
Duraciones en decimal, version 3 del protocolo.

### §4.3.3.1 — EXT-X-TARGETDURATION

Verbatim:

> The EXT-X-TARGETDURATION tag specifies the maximum Media Segment duration. The
> EXTINF duration of each Media Segment in the Playlist file, when rounded to the
> nearest integer, MUST be less than or equal to the target duration; longer
> segments can cause playback stalls. The EXT-X-TARGETDURATION tag is REQUIRED.

El constructor toma `ceil(max(dur))` y como minimo 4. Asi ninguna duracion
redondeada supera el objetivo.

### §4.3.3.2 — EXT-X-MEDIA-SEQUENCE

Verbatim:

> The EXT-X-MEDIA-SEQUENCE tag indicates the Media Sequence Number of the first
> Media Segment that appears in a Playlist file. [...] The EXT-X-MEDIA-SEQUENCE
> tag MUST appear before the first Media Segment in the Playlist file.

El constructor emite `#EXT-X-MEDIA-SEQUENCE` con el indice del primer segmento
del colchon, antes de los `#EXTINF`. Es lo que permite que un cliente que
reconecta no repita ni salte segmentos.

### Fuera de alcance

La bifurcacion HLS genera una **media playlist** en vivo. No genera master
playlist con `EXT-X-STREAM-INF`, no anuncia variantes de bitrate y no implementa
`EXT-X-ENDLIST` porque el flujo no termina. El camino de produccion de Jellyfin
no usa HLS: entrega MPEG-TS continuo por `/relay/<id>`.

---

## RFC 9110 — HTTP Semantics

Texto oficial: `https://www.rfc-editor.org/rfc/rfc9110.txt`.

### §15.5.4 — 403 Forbidden

Verbatim:

> The 403 (Forbidden) status code indicates that the server understood the
> request but refuses to authorize it.

El API devuelve 403 cuando el canal existe pero esta deshabilitado o tiene DRM.
Devuelve 404 cuando el id no existe. La diferencia importa: 403 dice "lo conozco
y no lo sirvo"; 404 dice "no lo conozco".

**Nota de consistencia:** la bifurcacion HLS todavia devuelve 404 para canal
deshabilitado o DRM. Es una diferencia conocida entre los dos caminos; el 403 es
el comportamiento de produccion y el que se documenta como correcto.

---

## XMLTV DTD

Fuente: `https://raw.githubusercontent.com/XMLTV/xmltv/master/xmltv.dtd`.

Declaraciones verificadas:

```
<!ELEMENT tv (channel*, programme*)>
<!ELEMENT channel (display-name+, icon*, url*) >
<!ATTLIST channel id CDATA #REQUIRED>
<!ELEMENT programme (title+, sub-title*, desc*, ...) >
<!ATTLIST programme start CDATA #REQUIRED
                    stop  CDATA #IMPLIED
                    channel CDATA #REQUIRED>
```

`channel` exige `id` y al menos un `display-name`; `programme` exige `start`,
`channel` y al menos un `title`; `stop` es opcional. `epg.validar_xmltv()` corre
antes de escribir: una guia que no valida no reemplaza a la anterior.

---

## Normas que se citan pero NO se imponen

- **M3U extendido**: no es un RFC. Es una convencion de la comunidad. El
  proyecto lo emite porque Jellyfin lo consume; no se presenta como norma.
- **JWT de Pluto**: es un token firmado, pero el puente no valida su firma. Solo
  lo transporta y lo renueva. No se cita RFC 7519 como conformidad.
- **AES-128-CBC**: lo descifra OpenSSL. El proyecto no implementa el cifrador;
  no se cita NIST SP 800-38A como si fuera codigo propio.

---

## Como se vuelve a verificar

```
# ISO: grep sobre el texto local
grep -n "continuity_counter" /home/server/tmp_lvb/norma/h222.txt

# RFC 8216: bajar y buscar la clausula
curl -s https://www.rfc-editor.org/rfc/rfc8216.txt | grep -n "EXT-X-TARGETDURATION"

# RFC 9110: idem
curl -s https://www.rfc-editor.org/rfc/rfc9110.txt | grep -n "403 (Forbidden)"

# Pruebas que fijan el comportamiento
cd /home/server/jellyfin-livetv-bridge && sudo -u server .venv/bin/python -m pytest -q
```

Regla de cambio: si se agrega una cita nueva, se agrega aqui con su seccion y su
evidencia. Si no se puede citar la seccion, se marca como MENCION y no se usa
para afirmar cumplimiento.
