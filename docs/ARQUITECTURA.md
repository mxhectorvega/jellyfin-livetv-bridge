# Arquitectura

El sistema se describe con el **modelo C4**: cuatro niveles que van de lo mas
general a lo mas concreto. Cada diagrama es la vista de un nivel; el texto de
abajo dice lo que el diagrama no cabe.

El proyecto es un **puente**: toma listas HLS de terceros y las convierte en algo
que Jellyfin pueda sintonizar como si fuera un tuner de antena, con guia y todo.

```
Nivel 1   Contexto         quien lo usa y de quien depende
Nivel 2   Contenedores     las piezas que corren, dentro y fuera
Nivel 3   Componentes      por dentro del rele HLS
Nivel 4   (no aplica)      el modelo C4 lo omite: dice que se genere
                           solo si aporta, y a nivel de clase no aporta
```

---

## Nivel 1 — Contexto

Quien usa el sistema, y de que otros sistemas depende.

```mermaid
C4Context
  title Nivel 1 - Contexto

  Person(espectador, "Espectador", "Ve la tele en Jellyfin: TV, movil o navegador")
  System(jellyfin, "Jellyfin", "Servidor de medios. Sintoniza los canales por su tuner tipo m3u")
  System(bridge, "jellyfin-livetv-bridge", "Publica la lista y la guia, y sirve cada canal como un flujo continuo")
  System_Ext(pluto, "Pluto TV (CDN)", "Origen HLS. Segmentos cifrados en AES-128, con URL firmada que caduca")
  System_Ext(guia, "Guia EPG", "XMLTV de la parrilla, publicada por i.mjh.nz")

  Rel(espectador, jellyfin, "Elige el canal y lo mira")
  Rel(jellyfin, bridge, "Pide la lista, la guia y el flujo del canal", "HTTP")
  Rel(bridge, pluto, "Baja el master y los segmentos de video y audio", "HTTPS")
  Rel(bridge, guia, "Baja la parrilla", "HTTPS")

  UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
  UpdateRelStyle(bridge, guia, $offsetX="-40", $offsetY="20")
```

Lo que hay que notar:

- **Jellyfin no habla con Pluto TV.** Solo habla con el puente. Si Pluto rota
  una URL o cambia su formato, Jellyfin no se entera: el que se apaña es el
  puente.
- **La guia y el video vienen de fuentes distintas**, y se empatan por el
  `tvg-id` del canal. En Pluto ese id es el ObjectId que aparece en la URL.
- El puente **no retransmite**: reemite. Baja los bytes, los ordena y los sirve
  continuos, que es justo lo que un origen que corta a comerciales no hace.

---

## Nivel 2 — Contenedores

Las piezas que corren y el estado que comparten.

```mermaid
C4Container
  title Nivel 2 - Contenedores

  Person(jellyfin, "Jellyfin", "Cliente del puente")

  Container_Boundary(puente, "jellyfin-livetv-bridge (un contenedor)") {
    Container(api, "API HTTP", "FastAPI y uvicorn", "Publica /playlist.m3u, /epg.xml, /status y atiende /relay/<id>")
    Container(pipeline, "Pipeline", "Python", "Ingesta, normaliza, sondea y escribe la lista y la guia")
    Container(rele, "Rele HLS", "Python, un hilo por canal", "Baja, descifra, muxea, cose y sirve con colchon propio")
    Container(salud, "Monitor de salud", "Python, hilo", "Sondea cada N segundos y habilita o deshabilita canales")
    ContainerDb(estado, "Estado", "JSON, M3U y XML en /data", "state.json, playlist.m3u, epg.xml, active_catalog.txt")
    ContainerDb(config, "Configuracion", "YAML en /config", "sources.yml: fuentes, respaldos y guia")
  }

  System_Ext(pluto, "Pluto TV (CDN)", "Origen HLS")
  System_Ext(guia, "Guia EPG", "XMLTV")

  Rel(jellyfin, api, "Pide la lista, la guia y el canal", "HTTP")
  Rel(api, rele, "Le pasa el canal y espera el flujo")
  Rel(rele, pluto, "Baja el master y los segmentos", "HTTPS")
  Rel(pipeline, pluto, "Valida que los canales respondan", "HTTPS")
  Rel(pipeline, guia, "Baja la parrilla", "HTTPS")
  Rel(pipeline, estado, "Escribe la lista, la guia y el estado")
  Rel(api, estado, "Lee el estado para /status")
  Rel(api, config, "Lee las fuentes")
  Rel(salud, estado, "Actualiza la salud de cada canal")

  UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

Lo que hay que notar:

- **El pipeline y el rele no comparten memoria, comparten disco.** El pipeline
  valida los canales y escribe `state.json`; el rele arranca un hilo por canal
  cuando alguien lo pide, no antes.
- **El estado vive en un bind mount** (`/data`), asi que sobrevive a recrear el
  contenedor. La configuracion tambien (`/config`).
- **El monitor de salud es independiente del pipeline**: puede deshabilitar un
  canal entre barridos, con backoff, sin esperar al siguiente.

---

## Nivel 3 — Componentes del rele

El rele es la pieza con mas partes moviles: por eso baja a este nivel y el resto
no.

```mermaid
C4Component
  title Nivel 3 - Componentes del rele (/relay/<id>)

  Person(jellyfin, "Jellyfin", "Pide el flujo del canal")

  Container_Boundary(rele, "RelayHLS") {
    Component(lector, "Lector", "Hilo propio", "Resuelve el master, elige la variante y baja cada segmento")
    Component(audio, "Rendicion de audio", "Playlist aparte", "Cuando el canal publica el audio separado del video")
    Component(descifra, "Descifrado", "openssl AES-128-CBC", "Quita el cifrado del segmento con la llave y el IV que le tocan")
    Component(muxeo, "Muxeo", "ts_mux.py", "Junta audio y video en un MPEG-TS: PAT, PMT, CRC_32 y PID estable")
    Component(cosido, "Cosido", "ts_splice.py", "Desplaza PCR, PTS y DTS al cambiar de fuente, y marca la discontinuidad")
    Component(colchon, "Colchon", "Memoria, ordenado", "Segmentos listos para servir, con tope y descarte de los viejos")
    Component(servidor, "Servidor de clientes", "Generador", "Le sirve a cada cliente lo que ya esta en el colchon")
  }

  ContainerDb(estado, "Estado", "state.json")
  System_Ext(pluto, "Pluto TV (CDN)", "HLS")
  System_Ext(openssl, "openssl", "Binario del sistema")

  Rel(jellyfin, servidor, "Se conecta a /relay/<id>", "HTTP")
  Rel(servidor, colchon, "Lee los segmentos ya listos")
  Rel(lector, pluto, "Baja el master y los segmentos", "HTTPS")
  Rel(lector, audio, "Resuelve la playlist de audio")
  Rel(lector, descifra, "Le pasa el segmento crudo")
  Rel(descifra, openssl, "Le invoca", "proceso")
  Rel(lector, muxeo, "Le pasa el par video y audio")
  Rel(muxeo, cosido, "Le pasa el TS ya unido")
  Rel(cosido, colchon, "Deja el segmento listo")
  Rel(lector, estado, "Deja su error y sus contadores")

  UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

Lo que hay que notar:

- **El lector y los clientes son hilos distintos, y el colchon es la frontera.**
  El lector nunca escribe directo a un cliente; los clientes nunca leen del
  origen. Esa separacion es lo que permite que un cliente entre a media emision
  sin perderse nada.
- **El descifrado y el muxeo son opcionales y se encadenan**: si el canal no
  viene cifrado, se salta el descifrado; si el audio viene en el mismo flujo, se
  salta el muxeo. Cuando el muxeo falla, se sirve el video solo y se cuenta en
  `sin_audio` en vez de tumbar el canal.
- **El cosido es lo que el proyecto vende**: al cambiar de fuente, el flujo
  sigue con la linea de tiempo continua en vez de terminar y que Jellyfin lo lea
  como fin de canal.

---

## Flujo de un canal, de punta a punta

Asi viaja un canal desde que existe una fuente hasta que se ve en una pantalla.

```mermaid
sequenceDiagram
  autonumber
  participant P as Pipeline
  participant S as state.json
  participant J as Jellyfin
  participant R as Rele
  participant T as Pluto TV

  P->>T: valida el canal (baja bytes, no metadatos)
  P->>S: deja el canal con su salud y su guia
  Note over S: playlist.m3u y epg.xml quedan escritos
  J->>P: pide /playlist.m3u y /epg.xml
  J->>R: abre /relay/<id>
  R->>T: resuelve el master y elige la variante
  loop cada segmento
    R->>T: baja el segmento de video y el de audio
    R->>R: descifra, muxea y cose
    R->>R: lo deja en el colchon
    R->>J: le sirve lo que ya esta listo
  end
  Note over R,T: si la fuente muere, salta a la respaldo<br/>y cose la linea de tiempo
```

---

## Por que esta forma y no otra

- **Un hilo por canal, no uno por cliente.** Un canal se baja UNA vez aunque lo
  vean diez personas. Un cliente de mas cuesta ancho de banda de salida, no una
  descarga nueva.
- **Colchon en memoria, no en disco.** Los segmentos de Pluto viven segundos.
  Escribirlos a disco seria desgaste puro para un dato que se tira.
- **El pipeline y el rele separados.** Si el sondeo tarda cinco minutos, eso no
  puede bloquear a quien ya esta viendo.
- **Estado en disco y no en una base de datos.** Son unos cientos de canales con
  un puñado de campos. Una base de datos seria mas piezas que mantener que datos
  que guardar.

Lo que el sistema **no** es: no es un transcodificador, no es un grabador, y no
guarda copia de ningun contenido. Reemite lo que ya esta publicado.
