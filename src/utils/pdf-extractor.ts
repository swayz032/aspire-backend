import { readFile } from 'fs/promises';
import { writeFile } from 'fs/promises';
import { existsSync } from 'fs';

// Import PDFParse class from pdf-parse
const { PDFParse } = require('pdf-parse');

/**
 * Interface for PDF page text content
 */
export interface PdfPage {
  pageNumber: number;
  text: string;
}

/**
 * Interface for complete PDF extraction result
 */
export interface PdfExtractionResult {
  totalPages: number;
  totalCharacters: number;
  pages: PdfPage[];
  fullText: string;
}

/**
 * Extracts text content from all pages of a PDF file
 *
 * @param filePath - Absolute path to the PDF file
 * @param options - Optional extraction options
 * @returns Promise resolving to complete PDF extraction result
 *
 * @example
 * ```typescript
 * const result = await extractPdfText('path/to/file.pdf');
 * console.log(`Total pages: ${result.totalPages}`);
 * console.log(`Page 1 text: ${result.pages[0].text}`);
 * ```
 */
export async function extractPdfText(filePath: string): Promise<PdfExtractionResult> {
  // Validate file exists
  if (!existsSync(filePath)) {
    throw new Error(`PDF file not found: ${filePath}`);
  }

  console.log(`📄 Reading PDF file: ${filePath}`);

  // Read PDF file as buffer
  const dataBuffer = await readFile(filePath);
  const fileSizeMB = (dataBuffer.length / (1024 * 1024)).toFixed(2);
  console.log(`📦 File size: ${fileSizeMB} MB`);

  console.log(`🔍 Parsing PDF content...`);

  // Create PDFParse instance with buffer data
  const parser = new PDFParse({ data: dataBuffer });

  try {
    // Extract text from all pages
    const result = await parser.getText();

    console.log(`✅ Parsed ${result.total} pages`);

    // Build structured result
    const pages: PdfPage[] = result.pages.map((page: any) => ({
      pageNumber: page.pageNumber,
      text: page.text.trim(),
    }));

    return {
      totalPages: result.total,
      totalCharacters: result.text.length,
      pages,
      fullText: result.text,
    };
  } finally {
    // Clean up parser resources
    await parser.destroy();
  }
}

/**
 * Extracts text from specific pages of a PDF
 *
 * @param filePath - Absolute path to the PDF file
 * @param pageNumbers - Array of page numbers to extract (1-based index)
 * @returns Promise resolving to extraction result with only specified pages
 *
 * @example
 * ```typescript
 * // Extract pages 1, 5, and 10
 * const result = await extractSpecificPages('file.pdf', [1, 5, 10]);
 * ```
 */
export async function extractSpecificPages(
  filePath: string,
  pageNumbers: number[]
): Promise<PdfExtractionResult> {
  // Validate file exists
  if (!existsSync(filePath)) {
    throw new Error(`PDF file not found: ${filePath}`);
  }

  const dataBuffer = await readFile(filePath);
  const parser = new PDFParse({ data: dataBuffer });

  try {
    // Extract only specified pages
    const result = await parser.getText({ partial: pageNumbers });

    const pages: PdfPage[] = result.pages.map((page: any) => ({
      pageNumber: page.pageNumber,
      text: page.text.trim(),
    }));

    return {
      totalPages: result.total,
      totalCharacters: result.text.length,
      pages,
      fullText: result.text,
    };
  } finally {
    await parser.destroy();
  }
}

/**
 * Saves extracted PDF text to a file
 *
 * @param result - PDF extraction result
 * @param outputPath - Path to save the text file
 */
export async function savePdfTextToFile(
  result: PdfExtractionResult,
  outputPath: string
): Promise<void> {
  let content = '';

  // Add header
  content += `# Extracted PDF Text\n\n`;
  content += `Total Pages: ${result.totalPages}\n`;
  content += `Total Characters: ${result.totalCharacters.toLocaleString()}\n\n`;
  content += `---\n\n`;

  // Add page-by-page content
  result.pages.forEach(page => {
    content += `## Page ${page.pageNumber}\n\n`;
    content += `${page.text}\n\n`;
    content += `---\n\n`;
  });

  await writeFile(outputPath, content, 'utf-8');
  console.log(`💾 Saved extracted text to: ${outputPath}`);
}
