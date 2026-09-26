# Datos JSONL de es.stackoverflow

Este directorio contiene dos variantes JSONL del *dump* de Stack Overflow en
español usado en el curso 2026-2027. La completa conserva todas las filas; en
ambas variantes sólo se truncan `Posts.Body`, `Comments.Text` y `Users.AboutMe`
a un máximo de 100 bytes UTF-8. Los datos proceden del Parquet de la release
`es.stackoverflow.data-26-27`.

## Contenido

| Fichero | Documentos | JSONL | gzip |
| --- | ---: | ---: | ---: |
| `Posts.jsonl.gz` | 419.881 | 258,1 MB | 41,55 MB |
| `Users.jsonl.gz` | 469.417 | 132,0 MB | 19,68 MB |
| `Comments.jsonl.gz` | 712.104 | 182,8 MB | 40,32 MB |
| `Votes.jsonl.gz` | 813.033 | 103,9 MB | 5,13 MB |
| `Tags.jsonl.gz` | 2.996 | 0,3 MB | 0,05 MB |
| **total** | **2.417.431** | **677,1 MB** | **106,73 MB** |

`manifest.json` registra los tamaños exactos, los recuentos, el origen y los
parámetros de generación. Todos los ficheros comprimidos quedan por debajo de
100 MB; el mayor ocupa 41.552.519 bytes.

## Variante reducida

Para equipos con menos memoria se conservan junto a los ficheros completos
cinco alternativas con el sufijo `-sample`, por ejemplo
`Posts-sample.jsonl.gz`; `manifest-sample.json` describe esta variante. También
procede del mismo Parquet y limita a 100 bytes UTF-8 los mismos campos de texto.
Mantiene una pregunta de cada ocho (`Id % 8 == 0`), todas sus respuestas,
comentarios y votos, los usuarios referenciados por esos registros y todas las
etiquetas. Tiene 246.898 documentos y ocupa 12,5 MB comprimida en total.

Las páginas web cargan por defecto los ficheros completos y permiten cambiar a
esta variante desde un botón. Sus consultas devuelven menos filas, porque se
trabaja con un conjunto reducido de hilos.

## Convenciones y transformación

El generador [`../preprocess/parquettojsonl.py`](../preprocess/parquettojsonl.py)
recorre todos los registros de cada Parquet. No aplica muestreo ni modifica
otros campos. Para las tres columnas de texto indicadas conserva el prefijo de
hasta 100 bytes UTF-8; si el corte cae dentro de un carácter, descarta sólo los
bytes incompletos finales para que el resultado siga siendo texto válido. No
añade puntos suspensivos.

Se conservan las convenciones de las sesiones 3 y 4 de BDGE:

- se mantiene el orden de columnas del Parquet y todas las columnas aparecen
en cada documento;
- los valores nulos se escriben como `null`, no como claves ausentes;
- las fechas se escriben en MongoDB Extended JSON (modo relajado), por ejemplo
  `{"$date":"2015-10-30T10:26:44.223Z"}`;
- no se genera `_id`.

Cada fichero es JSON Lines comprimido con gzip de nivel 9: un documento UTF-8
por línea. La marca temporal del gzip es cero, de modo que la generación con
los mismos Parquet produce los mismos bytes. El proceso lee el Parquet en lotes
de 20.000 filas y aborta si un fichero comprimido supera 95 MiB, dejando margen
bajo el límite de 100 MB por fichero de GitHub.

La carga completa en el navegador puede requerir varios GB de memoria, además
de la descarga de los cinco ficheros. Los resultados sí incluyen todas las
filas del *dump*, pero las consultas que dependan del contenido de esos tres
campos de texto operan sobre su versión truncada.

## Lectura desde una página web

Los *assets* de una *release* de GitHub no envían cabeceras CORS, así que una
página web debe descargar la copia versionada de este directorio. Las prácticas
prueban jsDelivr y usan `raw.githubusercontent.com` como alternativa. Las
descargas están separadas en dos *releases*: [JSONL completo de 2026-27](https://github.com/dsevilla/bd2-data/releases/tag/jsonl-full-26-27)
y [JSONL reducido de 2026-27](https://github.com/dsevilla/bd2-data/releases/tag/jsonl-sample-26-27).
Cada una publica sus ficheros con nombres ordinarios (`Posts.jsonl.gz`, etc.) y
un `manifest.json`. La completa sirve para descargar con `curl` o importar en
MongoDB:

```sh
gunzip -c Posts.jsonl.gz | mongoimport --db bdge --collection posts
```

Las fechas Extended JSON se pueden revivir como `Date` al leerlas desde
JavaScript. Por ejemplo, una colección se puede cargar línea a línea con
`JSON.parse(line, reviver)` después de pasar el flujo gzip por
`DecompressionStream("gzip")`.

## Regenerar y publicar

Desde la raíz de este repositorio:

```sh
make -C es.stackoverflow jsonl
make -C es.stackoverflow jsonl-sample
make -C es.stackoverflow jsonl FORCE=1
make -C es.stackoverflow jsonl-sample FORCE=1
```

Los *targets* descargan, si hace falta, los cinco Parquet de
`es.stackoverflow.data-26-27` y generan cada variante por separado. El límite
predeterminado es 100 bytes; se puede ajustar con `JSONL_TEXT_LIMIT_BYTES`,
aunque los ficheros deben mantenerse bajo el máximo por archivo.

El *workflow* [`update-jsonl-data`](../../.github/workflows/update-jsonl-data.yml)
regenera y confirma ambas variantes en el repositorio. Después publica dos
*releases*: `jsonl-full-26-27` contiene los ficheros completos y
`jsonl-sample-26-27` los reducidos. En cada una, los ficheros usan los nombres
habituales (`Posts.jsonl.gz`, etc.); el manifiesto de cada variante se publica
como `manifest.json`. El *workflow* conserva la etiqueta de la muestra para
mantener sus enlaces y reemplaza sus *assets* al regenerarla.

El origen del *dump* queda registrado en [`../source.json`](../source.json) y
en `manifest.json` y `manifest-sample.json`. Si el origen y los parámetros coinciden y los ficheros ya
están completos, `make jsonl` no los reconstruye. `FORCE=1` fuerza la
regeneración.
