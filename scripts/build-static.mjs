import { readFile, mkdir, cp, writeFile } from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';
import ejs from 'ejs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '..');
const distDir = path.join(rootDir, 'dist');

async function build() {
  console.log('Building static site for GitHub Pages...');

  await mkdir(distDir, { recursive: true });

  // Load books metadata with new schema (tags array max 3 + optional link)
  const metaPath = path.join(rootDir, 'meta_dt.json');
  const rawData = await readFile(metaPath, 'utf8');
  const books = JSON.parse(rawData);
  function normalizeTags(book) {
    let tags = [];
    if (Array.isArray(book.tags)) tags = book.tags;
    else if (typeof book.tag === 'string' && book.tag.trim()) tags = [book.tag];
    else if (typeof book.tags === 'string' && book.tags.trim()) tags = [book.tags];
    return tags.map(t => String(t).trim().toLowerCase()).filter(Boolean).slice(0, 3);
  }
  const bookList = Object.values(books).map(b => ({
    ...b,
    tags: normalizeTags(b),
    tag: normalizeTags(b)[0] || ''
  }));

  const defaultCategories = [
    'python','js','java','c','c++','go','rust','typescript',
    'git','os','cn','dbms','dsa','oop','math',
    'ml','ai','system-design','handwritten-notes','devops',
    'react','frontend','interview','roadmap','beginner','advanced','system','tricks'
  ];
  const dynamicTags = bookList.flatMap(b => b.tags || []);
  let allTags = Array.from(new Set([...defaultCategories, ...dynamicTags])).sort();
  const prioritized = ['handwritten-notes'];
  prioritized.forEach(p => {
    if (allTags.includes(p)) {
      const idx = allTags.indexOf(p);
      allTags.splice(idx, 1);
      allTags.unshift(p);
    }
  });

  // Render index.ejs to HTML
  const templatePath = path.join(rootDir, 'views', 'index.ejs');
  const html = await ejs.renderFile(templatePath, { books: bookList, tags: allTags });

  // Rewrite Express routes to static uploads paths for GitHub Pages.
  // Both /preview/ (inline view) and /download/ (attachment) resolve to the
  // same static file — GitHub Pages serves .pdf with inline disposition.
  const staticHtml = html
    .replaceAll('/preview/', 'uploads/')
    .replaceAll('/download/', 'uploads/');

  // Ensure links end with .pdf for static files
  // The EJS generates href="/download/<slug>" and download="<slug>.pdf"
  // We transform to href="uploads/<slug>.pdf"
  const finalHtml = staticHtml.replace(/href="uploads\/([^"]+)"/g, (match, slug) => {
    // if slug already ends with .pdf keep it, else add
    if (slug.endsWith('.pdf')) return match;
    return `href="uploads/${slug}.pdf"`;
  });

  await writeFile(path.join(distDir, 'index.html'), finalHtml, 'utf8');
  console.log(' -> dist/index.html generated');

  // Copy uploads (PDFs) to dist/uploads
  try {
    await cp(path.join(rootDir, 'uploads'), path.join(distDir, 'uploads'), { recursive: true });
    console.log(' -> uploads copied');
  } catch (e) {
    console.warn('No uploads to copy or copy failed:', e.message);
  }

  // Copy public assets if any
  try {
    await cp(path.join(rootDir, 'public'), path.join(distDir, 'public'), { recursive: true });
    console.log(' -> public copied');
  } catch (e) {
    // public may be empty
  }
  // Ensure style.css is at dist root for absolute /style.css link (Express serves public/style.css as /style.css)
  try {
    await cp(path.join(rootDir, 'public', 'style.css'), path.join(distDir, 'style.css'));
    console.log(' -> style.css copied to dist root');
  } catch (e) {
    // ignore
  }

  // Copy CNAME for custom domain (books.chizumizu.space) if present
  try {
    await cp(path.join(rootDir, 'CNAME'), path.join(distDir, 'CNAME'));
    console.log(' -> CNAME copied');
  } catch (e) {
    console.warn('No CNAME to copy:', e.message);
  }

  // Copy public contents to dist root as well (if public has assets)
  // Create .nojekyll to bypass Jekyll processing
  await writeFile(path.join(distDir, '.nojekyll'), '', 'utf8');

  // Create 404.html fallback (copy of index for SPA-like)
  await writeFile(path.join(distDir, '404.html'), finalHtml, 'utf8');

  console.log('Build complete: dist/');
}

build().catch(err => {
  console.error(err);
  process.exit(1);
});
