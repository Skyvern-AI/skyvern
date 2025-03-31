type Props = {
  title: string;
  image: string;
  onClick: () => void;
};

function WorkflowTemplateCard({ title, image, onClick }: Props) {
  return (
    <div className="h-48 w-56 cursor-pointer rounded-xl" onClick={onClick}>
      <div className="h-28 rounded-t-xl bg-slate-elevation1 px-6 pt-6">
        <img src={image} alt={title} className="h-full w-full object-contain" />
      </div>
      <div className="h-20 space-y-1 rounded-b-xl bg-slate-elevation2 p-3">
        <h1
          className="overflow-hidden text-ellipsis whitespace-nowrap"
          title={title}
        >
          {title}
        </h1>
        <p className="text-sm text-slate-400">Template</p>
      </div>
    </div>
  );
}

export { WorkflowTemplateCard };
