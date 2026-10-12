# DISCLAIMER / AVISO LEGAL

**jellyfin-livetv-bridge** es una herramienta de uso PERSONAL y PRIVADO.

## Que hace y que NO hace

Este software agrega, valida, normaliza y sirve por HTTP listas de canales
(M3U/HLS) que **el usuario aporta por su cuenta**. NO incluye, NO distribuye y
NO enlaza ninguna lista de streams real: el repositorio solo contiene
`config/sources.example.yml`, con URLs ficticias (`example.invalid`).

## Responsabilidad del usuario

- Las URLs, credenciales, tokens y listas que se carguen en `/config` son
  responsabilidad exclusiva del usuario.
- Es responsabilidad del usuario contar con autorizacion para acceder a los
  contenidos que configure. La disponibilidad publica de una URL **no**
  implica una licencia de uso ni de redistribucion.
- El autor no se hace responsable del uso indebido del software ni de
  reclamaciones de terceros derivadas de los contenidos servidos.

## Contenido protegido

- El software **no elude ni descifra** protecciones de contenido. Los flujos
  con DRM (Widevine/PlayReady), cifrado CENC (`senc`/`cenc`) o cifrado HLS
  (`EXT-X-KEY`) se **detectan y se marcan**, pero **no se reproducen**: una
  copia de flujo (`-c copy`) no descifra. Esto es una limitacion de diseno, no
  un pendiente.
- No se debe usar este software para saltar medidas tecnologicas de proteccion.

## Sin garantia

Software entregado "TAL CUAL", sin garantia de ningun tipo. Ver `LICENSE`
(MIT). El autor no garantiza disponibilidad, exactitud ni continuidad de
ninguna fuente externa.

## Privacidad

El servicio registra URLs y metadatos en sus logs JSON (`/data`). Si esas URLs
contienen tokens firmados o credenciales, el usuario debe proteger
`/data` (permisos, respaldo, rotacion) y **no compartir sus logs**.
