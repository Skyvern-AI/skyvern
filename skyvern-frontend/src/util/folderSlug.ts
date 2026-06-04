type SluggableFolder = {
  folder_id: string;
  title: string;
  created_at: string;
};

function slugifyFolderName(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^\w\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

// Append a numeric suffix only when multiple folders slugify to the same base,
// assigning suffixes by created_at order so a folder's slug stays stable.
function getUniqueSlugForFolder<T extends SluggableFolder>(
  folder: T,
  allFolders: T[],
): string {
  const baseSlug = slugifyFolderName(folder.title);

  // Titles made only of stripped characters (emoji, punctuation, non-Latin)
  // slugify to "", which the page reads as "no folder selected". Fall back to
  // the unique folder id so the folder stays selectable and deep-linkable.
  if (!baseSlug) {
    return folder.folder_id;
  }

  const foldersWithSameSlug = allFolders.filter(
    (f) => slugifyFolderName(f.title) === baseSlug,
  );

  if (foldersWithSameSlug.length <= 1) {
    return baseSlug;
  }

  const sortedFolders = [...foldersWithSameSlug].sort(
    (a, b) =>
      new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  );

  const index = sortedFolders.findIndex(
    (f) => f.folder_id === folder.folder_id,
  );

  return index === 0 ? baseSlug : `${baseSlug}-${index + 1}`;
}

export { slugifyFolderName, getUniqueSlugForFolder };
