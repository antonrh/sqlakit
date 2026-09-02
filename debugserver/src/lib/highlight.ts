/**
 * Colouring, for the two languages this page shows.
 *
 * A full grammar was tried and dropped: the one for SQL knows neither `OFFSET`
 * nor `RETURNING` nor any of the four spellings a driver gives a parameter, and
 * it weighs a third of a megabyte. What arrives here is SQL a mapper wrote, and
 * the parts worth telling apart are few.
 */

const KEYWORDS =
  "SELECT|INSERT INTO|INSERT|UPDATE|DELETE|FROM|WHERE|AND|OR|NOT|NULL|IS|IN|AS|ON|SET" +
  "|VALUES|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|CROSS|LATERAL|GROUP BY|ORDER BY|HAVING" +
  "|LIMIT|OFFSET|FETCH|DISTINCT|COUNT|EXISTS|BETWEEN|LIKE|ILIKE|CASE|WHEN|THEN|ELSE" +
  "|END|UNION ALL|UNION|INTERSECT|EXCEPT|WITH|RECURSIVE|RETURNING|ASC|DESC" +
  "|NULLS FIRST|NULLS LAST|COLLATE|ON CONFLICT|DO UPDATE|DO NOTHING|EXCLUDED|OVER" +
  "|PARTITION BY|WINDOW|FILTER|SAVEPOINT|RELEASE|PRAGMA|USING|CAST"

const SQL = new RegExp(
  "(/\\*[\\s\\S]*?\\*/|--[^\\n]*)" + // a comment, the template's name among them
    "|('(?:[^']|'')*')" + // a string
    "|(:[a-zA-Z_]\\w*|%\\([^)]*\\)s|\\$\\d+|\\?)" + // a parameter
    "|\\b(\\d+(?:\\.\\d+)?)\\b" + // a number
    `|\\b(${KEYWORDS})\\b`,
  "gi",
)

const PYTHON = new RegExp(
  "(#[^\\n]*)" + // a comment
    "|('(?:[^'\\\\]|\\\\.)*'|\"(?:[^\"\\\\]|\\\\.)*\")" + // a string
    "|(:[a-zA-Z_]\\w*)" + // nothing: keeps the groups lined up with SQL
    "|\\b(\\d+(?:\\.\\d+)?)\\b" + // a number
    "|\\b(with|as|import|from|def|class|return|None|True|False|for|in|if|else)\\b",
)

const CLASSES = ["comment", "string", "parameter", "number", "keyword"]

const safe = (text: string) =>
  text.replace(/[&<>]/g, (one) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[one] as string)

/** The code as HTML, each part in a span the stylesheet colours. */
export function coloured(code: string, lang: "sql" | "python" = "sql"): string {
  const grammar = new RegExp(lang === "sql" ? SQL : PYTHON, "gi")
  return safe(code).replace(grammar, (match, ...groups: (string | undefined)[]) => {
    const which = groups.slice(0, CLASSES.length).findIndex((group) => group !== undefined)
    return which < 0 ? match : `<span class="tok-${CLASSES[which]}">${match}</span>`
  })
}
