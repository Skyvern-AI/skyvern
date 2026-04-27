const UseCase = ({ title, description, href }) => {
  return (
    <a
      href={href}
      style={{
        display: "block",
        padding: "20px",
        borderRadius: "10px",
        border: "1px solid #E5DDD4",
        backgroundColor: "#fff",
        textDecoration: "none",
        color: "inherit",
        transition: "border-color 0.15s ease, box-shadow 0.15s ease",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "#7BA3A5";
        e.currentTarget.style.boxShadow = "0 4px 12px rgba(0,0,0,0.06)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "#E5DDD4";
        e.currentTarget.style.boxShadow = "none";
      }}
    >
      <h4
        style={{
          fontSize: "14px",
          fontWeight: 600,
          color: "#1a1a1a",
          margin: "0 0 4px 0",
        }}
      >
        {title}
      </h4>
      <p
        style={{
          fontSize: "13px",
          color: "#6B6560",
          margin: 0,
          lineHeight: 1.5,
        }}
      >
        {description}
      </p>
    </a>
  );
};

export const UseCaseGrid = () => {
  return (
    <div>
      <h3
        style={{
          fontSize: "15px",
          fontWeight: 600,
          color: "#6B6560",
          textTransform: "uppercase",
          letterSpacing: "0.05em",
          margin: "0 0 12px 0",
        }}
      >
        Popular use cases
      </h3>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
          gap: "12px",
        }}
      >
        <UseCase
          title="Download invoices"
          description="Log into vendor portals, find invoices, download PDFs."
          href="/cookbooks/bulk-invoice-downloader"
        />
        <UseCase
          title="Fill forms at scale"
          description="Submit applications, registrations, and compliance forms."
          href="/cookbooks/job-application-filler"
        />
        <UseCase
          title="Extract data from websites"
          description="Pull structured data from any site without an API."
          href="/running-automations/extract-structured-data"
        />
        <UseCase
          title="Healthcare portal automation"
          description="Extract patient demographics and billing data from EHR portals."
          href="/cookbooks/healthcare-portal-data"
        />
      </div>
    </div>
  );
};
