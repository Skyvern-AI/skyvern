type Props = {
  title: string;
  image: string;
  onClick: () => void;
  description?: string;
  onSave?: () => void;
  showSaveButton?: boolean;
};

function WorkflowTemplateCard({
  title,
  image,
  onClick,
  description,
  onSave,
  showSaveButton = false,
}: Props) {
  return (
    <div className="h-56 w-56 cursor-pointer rounded-xl">
      <div
        className="h-28 rounded-t-xl bg-slate-elevation1 px-6 pt-6"
        onClick={onClick}
      >
        <img src={image} alt={title} className="h-full w-full object-contain" />
      </div>
      <div className="h-28 space-y-2 rounded-b-xl bg-slate-elevation2 p-3">
        <h1
          className="overflow-hidden text-ellipsis whitespace-nowrap font-medium"
          title={title}
          onClick={onClick}
        >
          {title}
        </h1>
        {description ? (
          <p
            className="line-clamp-2 text-xs text-slate-400"
            title={description}
            onClick={onClick}
          >
            {description}
          </p>
        ) : (
          <p className="text-sm text-slate-400" onClick={onClick}>
            Template
          </p>
        )}

        {showSaveButton && onSave && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onSave();
            }}
            className="mt-2 w-full rounded bg-blue-600 px-3 py-1.5 text-sm text-white transition-colors hover:bg-blue-700"
          >
            Save to Workflows
          </button>
        )}
      </div>
    </div>
  );
}

export { WorkflowTemplateCard };
