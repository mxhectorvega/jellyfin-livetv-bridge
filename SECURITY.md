# Politica de seguridad

## Como reportar

Usa el **reporte privado de vulnerabilidades de GitHub**: en este repositorio,
`Security` > `Report a vulnerability`. No abras un issue publico para un fallo de
seguridad.

Si el problema es que un **secreto o dato tuyo** (una URL con token, una
credencial, una IP de tu red) acabo en un archivo versionado, tratalo igual: el
historial de git lo conserva, asi que hay que **rotar el secreto**, no solo
borrar el archivo.

## Alcance

Cuenta como fallo:

- Ejecucion de codigo, o lectura/escritura de archivos fuera de `/config` y
  `/data`, a traves de una fuente M3U, de una guia XMLTV o de la API.
- Evasion de DRM/CENC. El proyecto **no descifra contenido protegido** (ver
  `DISCLAIMER.md`); si algo lo consigue, es un fallo.
- Eludir `LVB_API_TOKEN` en los `POST`, o inyectar en la salida
  (`playlist.m3u`, `epg.xml`) mediante nombres, logos o rutas de una fuente.
- Que `.env`, `config/sources.yml` o `data/` terminen en la imagen o en el
  contexto de build.

No cuenta como fallo, porque es comportamiento declarado:

- Que un canal con DRM no se reproduzca.
- Que una fuente externa deje de responder.
- Que los `GET` (`/status`, `/playlist.m3u`) queden sin autenticacion: los
  consume Jellyfin sin cabeceras y solo exponen metadatos; `/channels` NO
  publica las URLs de origen.

## Lo que no se acepta

Parches que anadan evasion de protecciones de contenido, captura de
credenciales, o listas de streams reales al repositorio.
