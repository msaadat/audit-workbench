/**
 * Print one document sheet, and nothing else on the page.
 *
 * The sheet lives three scroll containers deep in a fixed-height shell, beside
 * an outline and a rail; print the page as it stands and the browser prints
 * the shell, clips the document at the first scroller, and leaves the rest
 * off. So the sheet is copied to the top of `<body>`, the print stylesheet
 * shows that copy alone (`html.aw-printing` in style.css), and the copy is
 * removed when the dialog closes. Scoped styles travel with the copy — they
 * are attribute selectors, and `cloneNode` keeps the attributes.
 *
 * Paper is light whatever the screen is: the theme is held at light for the
 * length of the dialog, through the same `data-theme` the theme switch sets.
 */
export function printDocument(sheet: Element | null | undefined): void {
  if (!sheet) return
  const root = document.createElement('div')
  root.className = 'aw-print-root'
  root.appendChild(sheet.cloneNode(true))

  const html = document.documentElement
  const theme = html.getAttribute('data-theme')
  html.setAttribute('data-theme', 'light')
  html.classList.add('aw-printing')
  document.body.appendChild(root)

  let finished = false
  const finish = () => {
    if (finished) return
    finished = true
    window.removeEventListener('afterprint', finish)
    root.remove()
    html.classList.remove('aw-printing')
    if (theme === null) html.removeAttribute('data-theme')
    else html.setAttribute('data-theme', theme)
  }
  window.addEventListener('afterprint', finish)
  window.print()
}

/** The sheet inside a page, for the page's Print button. */
export function printSheetIn(root: Element | null | undefined): void {
  printDocument(root?.querySelector('.doc-sheet'))
}
