# Parquet del curso 25-26 (heredado)

Datos y generador del curso anterior. **No son los datos actuales**: los del
curso 26-27 están en [`../parquet/`](../parquet/).

- `gen_parquet.ipynb` genera los ficheros a partir del XML del volcado de 2025,
  que descarga de `../es.stackoverflow.xml.tar.xz.*` (por eso ese fichero sigue en
  el repositorio). Lo ejecuta el workflow
  [`update-parquet-files.yml`](../../.github/workflows/update-parquet-files.yml)
  para regenerar la release `parquet-files` (sin año en el nombre: la usan las
  asignaturas que aún no han pasado a los datos nuevos).
- `Posts1..3.parquet`, `Users`, `Tags`, `Comments`, `Votes` y los esquemas `*.pb`
  (esquema PyArrow serializado) son los que lee
  [`../mysql/generate-mysql-db.ipynb`](../mysql/generate-mysql-db.ipynb) para
  construir el volcado de MySQL.

No los muevas ni los regeneres sin actualizar esas dos rutas.
