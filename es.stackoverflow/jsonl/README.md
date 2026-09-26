# Muestra JSONL de es.stackoverflow

Muestra reducida del *dump* de Stack Overflow en español, pensada para
practicar consultas **dentro del navegador** con un motor de consultas MongoDB
en memoria como [mingo](https://github.com/kofrasa/mingo), sin servidor y sin
instalar nada.

El *dump* completo ocupa alrededor de 1 GB descomprimido y no cabe en la
memoria de una pestaña: sólo `Posts.Body` son 792 MB, el 96 % del total. Esta
muestra ocupa **12,5 MB comprimidos** y unos 100 MB de *heap* una vez cargada
en JavaScript.

## Contenido

| Fichero | Documentos | JSONL | gzip |
| --- | ---: | ---: | ---: |
| `Posts.jsonl.gz` | 55.884 | 34,4 MB | 5,5 MB |
| `Comments.jsonl.gz` | 88.831 | 23,0 MB | 5,2 MB |
| `Votes.jsonl.gz` | 77.162 | 9,8 MB | 0,5 MB |
| `Users.jsonl.gz` | 22.025 | 6,5 MB | 1,3 MB |
| `Tags.jsonl.gz` | 2.996 | 0,3 MB | 0,05 MB |
| **total** | **246.898** | **74,1 MB** | **12,5 MB** |

`manifest.json` repite estas cifras junto con los parámetros con los que se
generó la muestra, para que una página pueda mostrarlas sin abrir los datos.

## Cómo se construye la muestra

Dos reducciones independientes, ambas en
[`../preprocess/parquettojsonl.py`](../preprocess/parquettojsonl.py):

- **Texto truncado**: `Posts.Body`, `Comments.Text` y `Users.AboutMe` se cortan
  a 100 caracteres. Un valor cortado termina en `…`, de modo que se distingue
  de uno naturalmente corto.
- **Muestreo por hilos, no aleatorio**: se conserva una pregunta de cada 8
  (`Id % 8 == 0`) y, con ella, **todas** sus respuestas, comentarios y votos, y
  todos los usuarios a los que esas filas hacen referencia. `Tags` se conserva
  entero, junto con los posts de extracto y wiki que referencia.

Muestrear hilos completos es lo que mantiene los ejercicios con sentido: con un
muestreo aleatorio de posts habría respuestas sin su pregunta, y los `$lookup`
devolverían arrays vacíos. Sobre los ficheros generados se comprueba que no
queda ninguna referencia colgando: `OwnerUserId`, `LastEditorUserId`,
`ParentId`, `AcceptedAnswerId`, `PostId`, `UserId`, `ExcerptPostId` y
`WikiPostId` resuelven siempre dentro de la muestra.

## Convenciones de los documentos

Son las mismas que usan las sesiones 3 y 4 de BDGE al cargar los Parquet con
`RecordBatch.to_pylist()`:

- se mantiene el orden de columnas del Parquet y **todas** las columnas
  aparecen en todos los documentos;
- lo ausente es `null`, no una clave que falta (así siguen teniendo sentido los
  ejercicios sobre `$exists` frente a `null`);
- las fechas van en *MongoDB Extended JSON* (modo relajado),
  `{"$date": "2015-10-30T10:26:44.223Z"}`, que entiende tanto `mongoimport`
  como el `reviver` de la página;
- no se genera `_id`: lo pone el servidor, o el propio cargador.

Cada fichero es JSON Lines comprimido con gzip: un documento por línea, UTF-8.
El gzip se escribe con marca de tiempo cero, así que regenerar la muestra sin
cambios en los datos produce ficheros byte a byte idénticos y no crea
*commits* vacíos.

## Cargarla en el navegador

Los *assets* de un *release* de GitHub **no envían cabeceras CORS**, así que
desde una página hay que leer la copia versionada en este repositorio, que sí
las envía:

```js
// jsDelivr (CDN, CORS, límite de 20 MB por fichero; el mayor aquí son 5,5 MB)
const BASE = "https://cdn.jsdelivr.net/gh/dsevilla/bd2-data@main/es.stackoverflow/jsonl";
// Alternativa sin CDN: https://raw.githubusercontent.com/dsevilla/bd2-data/main/es.stackoverflow/jsonl

const revivir = (clave, valor) =>
  valor !== null && typeof valor === "object" && typeof valor.$date === "string"
    ? new Date(valor.$date)
    : valor;

async function cargar(tabla) {
  const respuesta = await fetch(`${BASE}/${tabla}.jsonl.gz`);
  // El fichero llega tal cual (sin Content-Encoding), así que se descomprime aquí.
  const texto = await new Response(
    respuesta.body.pipeThrough(new DecompressionStream("gzip")),
  ).text();
  return texto.split("\n").filter(Boolean).map((linea) => JSON.parse(linea, revivir));
}

const db = {};
for (const tabla of ["Posts", "Users", "Comments", "Votes", "Tags"]) {
  db[tabla.toLowerCase()] = await cargar(tabla);
}

// Las colecciones son arrays; $lookup las resuelve por nombre.
const opciones = { collectionResolver: (nombre) => db[nombre] };
const respuestasPorPregunta = new mingo.Aggregator([
  { $match: { PostTypeId: 1 } },
  { $lookup: { from: "posts", localField: "Id", foreignField: "ParentId", as: "respuestas" } },
  { $addFields: { NumRespuestas: { $size: "$respuestas" } } },
  { $sort: { NumRespuestas: -1 } },
  { $limit: 10 },
  { $project: { _id: 0, Id: 1, Title: 1, NumRespuestas: 1 } },
], opciones).run(db.posts);
```

Medido con mingo 7.2.4 sobre esta muestra: 0,6 s de carga, ~100 MB de *heap* y
entre 5 y 40 ms por consulta, incluidos los `$lookup` anteriores.

Dos avisos para quien escriba los ejercicios:

- `$lookup` con `localField`/`foreignField` construye una tabla *hash* de la
  colección unida, pero `$lookup` con `let` + `pipeline` ejecuta el
  subpipeline **una vez por documento de entrada**: es cuadrático y bloquea la
  pestaña. Conviene forzar la primera forma, o poner un `$limit` antes.
- mingo no tiene índices ni `explain`, así que los ejercicios sobre planes de
  consulta e índices siguen necesitando un `mongod` de verdad.

## Cargarla en un MongoDB real

El mismo fichero sirve para un `mongod`, con las fechas ya tipadas:

```sh
gunzip -c Posts.jsonl.gz | mongoimport --db bdge --collection posts
```

## Regenerar

Desde `es.stackoverflow/`:

```sh
make jsonl                                   # descarga los .parquet del release y genera jsonl/
make jsonl THREAD_MODULO=4 TEXT_LIMIT=200    # el doble de hilos y más texto
```

Los Parquet se descargan del *release* `es.stackoverflow.data-26-27`, no de los
ficheros de este repositorio. El generador falla si algún fichero de salida
supera los 95 MB, el tamaño a partir del cual GitHub rechaza el *push*; hoy el
mayor son 5,5 MB, así que no hace falta partirlos como sí ocurre con
`es.stackoverflow.db.xz.00/.01`.

El *workflow* [`update-jsonl-sample`](../../.github/workflows/update-jsonl-sample.yml)
hace lo mismo en CI, publica los ficheros como *release* `jsonl-sample-26-27` y
confirma en el repositorio los que hayan cambiado.

No reconstruye la muestra si no ha cambiado el origen:
[`../source.json`](../source.json) dice de qué volcado de Stack Exchange sale el
*release* (URL y sha256 del `.7z`; lo escribe y lo commitea el *workflow* que
lo genera, y también se publica con él), y `manifest.json` guarda el del último
`make jsonl`. Si coinciden, los
parámetros son los mismos y los ficheros están, no se hace nada. `make jsonl
FORCE=1` (o la opción `force` del *workflow*) la reconstruye de todos modos.
