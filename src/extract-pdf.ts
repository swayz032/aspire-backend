#!/usr/bin/env node

/**
 * PDF Text Extraction Script
 *
 * Extracts all text content from a PDF file and saves it to a text file.
 *
 * Usage:
 *   npx ts-node src/extract-pdf.ts <path-to-pdf> [output-file]
 *
 * Examples:
 *   npx ts-node src/extract-pdf.ts "./plan/Aspire ecosystem .pdf"
 *   npx ts-node src/extract-pdf.ts "./plan/Aspire ecosystem .pdf" "./plan/output.txt"
 */

import { resolve, basename, dirname, join } from 'path';
import { extractPdfText, savePdfTextToFile } from './utils/pdf-extractor';

async function main() {
  const args = process.argv.slice(2);

  if (args.length === 0) {
    console.error('❌ Error: No PDF file specified');
    console.log('\nUsage:');
    console.log('  npx ts-node src/extract-pdf.ts <path-to-pdf> [output-file]');
    console.log('\nExamples:');
    console.log('  npx ts-node src/extract-pdf.ts "./plan/Aspire ecosystem .pdf"');
    console.log('  npx ts-node src/extract-pdf.ts "./plan/Aspire ecosystem .pdf" "./plan/output.txt"');
    process.exit(1);
  }

  const pdfPath = resolve(args[0]);
  const outputPath = args[1]
    ? resolve(args[1])
    : join(dirname(pdfPath), `${basename(pdfPath, '.pdf')}-extracted.txt`);

  console.log('🚀 PDF Text Extraction Started');
  console.log('================================\n');

  const startTime = Date.now();

  try {
    // Extract PDF content
    const result = await extractPdfText(pdfPath);

    // Display summary
    console.log('\n📊 Extraction Summary:');
    console.log(`   Total Pages: ${result.totalPages}`);
    console.log(`   Total Characters: ${result.totalCharacters.toLocaleString()}`);

    // Display first few pages as preview
    console.log('\n📖 Preview (First 3 pages):');
    result.pages.slice(0, 3).forEach(page => {
      const preview = page.text.substring(0, 150).replace(/\n/g, ' ');
      console.log(`\n   Page ${page.pageNumber}: ${preview}...`);
    });

    // Save to file
    console.log('\n💾 Saving extracted text...');
    await savePdfTextToFile(result, outputPath);

    const duration = ((Date.now() - startTime) / 1000).toFixed(2);
    console.log(`\n✅ Extraction completed in ${duration}s`);
    console.log(`📁 Output saved to: ${outputPath}`);

    // Display access instructions
    console.log('\n📚 Access the extracted text:');
    console.log(`   - Full text: result.fullText`);
    console.log(`   - By page: result.pages[0].text (page 1)`);
    console.log(`   - Page count: result.totalPages`);

  } catch (error) {
    console.error('\n❌ Extraction failed:');
    console.error(`   ${error instanceof Error ? error.message : String(error)}`);
    process.exit(1);
  }
}

// Run the script
main().catch(error => {
  console.error('Fatal error:', error);
  process.exit(1);
});
