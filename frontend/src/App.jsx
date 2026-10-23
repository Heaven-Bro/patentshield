import React from "react";
import WorkspaceLayout from "./components/WorkspaceLayout";

export default function App() {
  // টেস্ট করার জন্য একটি পাবলিক স্যাম্পল PDF URL ব্যবহার করা হচ্ছে
  const samplePdfUrl =
    "https://raw.githubusercontent.com/mozilla/pdf.js/ba2edeae/examples/learning/helloworld.pdf";

  return (
    <div className="h-screen w-screen overflow-hidden bg-gray-900 text-white">
      <WorkspaceLayout pdfFile={samplePdfUrl} />
    </div>
  );
}