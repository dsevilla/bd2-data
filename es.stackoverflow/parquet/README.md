# Parquet de es.stackoverflow (curso 26-27)

Las cinco tablas del volcado de es.stackoverflow.com del 30 de junio de 2026,
las mismas que la release `es.stackoverflow.data-26-27` (que es la fuente de
verdad: ver [`../preprocess/README.md`](../preprocess/README.md)).

| Fichero | Filas |
| --- | ---: |
| `Posts1.parquet`, `Posts2.parquet`, `Posts3.parquet` | 419.881 |
| `Users.parquet` | 469.417 |
| `Comments.parquet` | 712.104 |
| `Votes.parquet` | 813.033 |
| `Tags.parquet` | 2.996 |

## Por qué están en el repositorio

Los ficheros de una release se descargan sin cabeceras CORS, así que una página
web no puede leerlos. Los del repositorio se sirven desde
`raw.githubusercontent.com`, que sí las envía:

```
https://raw.githubusercontent.com/dsevilla/bd2-data/main/es.stackoverflow/parquet/Users.parquet
```

## `Posts` está en tres trozos

GitHub rechaza ficheros de más de 100 MB y `Posts.parquet` ocupa 171 MB. Los
trozos se cortan entre *row groups* y siguen ordenados por `Id`: leerlos uno tras
otro da la tabla completa (`Posts1` tiene los `Id` más bajos, `Posts3` los más
altos). Con DuckDB, por ejemplo:

```sql
SELECT count(*) FROM read_parquet([
  'https://raw.githubusercontent.com/dsevilla/bd2-data/main/es.stackoverflow/parquet/Posts1.parquet',
  'https://raw.githubusercontent.com/dsevilla/bd2-data/main/es.stackoverflow/parquet/Posts2.parquet',
  'https://raw.githubusercontent.com/dsevilla/bd2-data/main/es.stackoverflow/parquet/Posts3.parquet']);
```

El esquema (tipos, nulabilidad, claves primarias y ajenas) va dentro de cada
fichero; no hay ficheros `.pb` aparte.

## Regeneración

No se editan a mano. Desde `es.stackoverflow/`, `make parquet` descarga los
ficheros de la release, trocea `Posts` con
[`../preprocess/splitparquet.py`](../preprocess/splitparquet.py) y comprueba que
los trozos son idénticos a la tabla original. El workflow
[`update-repo-parquet.yml`](../../.github/workflows/update-repo-parquet.yml) hace
lo mismo y sube el resultado; solo se lanza a mano.

Los ficheros del curso 25-26 y su generador están en [`../parquet.old/`](../parquet.old/).
