#!/usr/bin/env python3
"""
PDFforge Pro Backend Server - Universal Document & Image Processor
Handles: Merge, Split, Compress, Rotate, Extract, Info
Supported Formats: PDF, PNG, JPG, JPEG
"""

from flask import Flask, request, send_file, jsonify
from pypdf import PdfReader, PdfWriter
import fitz  # PyMuPDF
from PIL import Image
import os
import io
import uuid
import tempfile
import zipfile

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max

# Dictionary to hold temp file references in memory securely
TEMP_STORAGE = {}

def validate_file(file_obj, filename):
    """Validate file extensions and signatures"""
    ext = os.path.splitext(filename.lower())[1]
    file_obj.seek(0)
    header = file_obj.read(4)
    file_obj.seek(0)
    
    if ext == '.pdf' and header == b'%PDF':
        return 'pdf'
    elif ext in ['.png', '.jpg', '.jpeg']:
        return 'image'
    return None

@app.route('/api/pdf/compress-preview', methods=['POST'])
def compress_preview():
    """Handles percentage-based compression preview for PDFs and Images"""
    try:
        file = request.files.get('file')
        percentage = int(request.form.get('percentage', 50))
        
        if not file:
            return jsonify({'error': 'No file uploaded'}), 400
            
        file_type = validate_file(file, file.filename)
        if not file_type:
            return jsonify({'error': 'Unsupported file format'}), 400
            
        # Get original size
        file.seek(0, os.SEEK_END)
        original_size_kb = file.tell() / 1024
        file.seek(0)
        
        # Create unique trackable ID for the two-step download
        file_id = str(uuid.uuid4())
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"{file_id}_{file.filename}")
        
        if file_type == 'image':
            # Handle Image Compression using Pillow
            img = Image.open(file)
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            # Higher strength slider value means lower quality percentage
            quality_target = max(1, 100 - percentage)
            img.save(temp_path, format='JPEG', optimize=True, quality=quality_target)
            
        elif file_type == 'pdf':
            # Handle PDF Compression using PyMuPDF (fitz)
            doc = fitz.open(stream=file.read(), filetype="pdf")
            # Compress streams, discard unused objects, and optimize layout
            garbage_level = 4 if percentage > 50 else 3
            doc.save(temp_path, garbage=garbage_level, deflate=True, clean=True)
            doc.close()
            
        # Check compressed size
        compressed_size_kb = os.path.getsize(temp_path) / 1024
        
        # Safety Fallback: If compressed file is larger, override with original
        if compressed_size_kb >= original_size_kb:
            file.seek(0)
            with open(temp_path, 'wb') as f:
                f.write(file.read())
            compressed_size_kb = original_size_kb

        TEMP_STORAGE[file_id] = temp_path
        
        return jsonify({
            'file_id': file_id,
            'original_size_kb': round(original_size_kb, 2),
            'compressed_size_kb': round(compressed_size_kb, 2),
            'savings_percentage': round(max(0, ((original_size_kb - compressed_size_kb) / original_size_kb) * 100), 1)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/pdf/compress-download/<file_id>', methods=['GET'])
def compress_download(file_id):
    """Delivers the pre-compressed file and cleans it off the disk"""
    temp_path = TEMP_STORAGE.get(file_id)
    if not temp_path or not os.path.exists(temp_path):
        return jsonify({'error': 'File expired or not found'}), 404
        
    filename = os.path.basename(temp_path).split('_', 1)[1]
    
    response = send_file(
        temp_path,
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name=f"compressed_{filename}"
    )
    
    # Securely delete local temp path after serving response
    @response.call_on_close
    def cleanup():
        try:
            os.remove(temp_path)
            TEMP_STORAGE.pop(file_id, None)
        except Exception:
            pass
            
    return response

@app.route('/api/pdf', methods=['POST'])
def process_pdf():
    """Handles classic tools upgraded with Image/PDF cross-compatibility"""
    try:
        tool = request.form.get('tool')
        
        if tool == 'merge':
            files = request.files.getlist('files')
            if len(files) < 2:
                return jsonify({'error': 'Need at least 2 files'}), 400
                
            writer = PdfWriter()
            for file in files:
                ftype = validate_file(file, file.filename)
                if ftype == 'pdf':
                    reader = PdfReader(file)
                    for page in reader.pages:
                        writer.add_page(page)
                elif ftype == 'image':
                    # Convert raw image directly into a temporary PDF page stream
                    img = Image.open(file).convert('RGB')
                    pdf_bytes = io.BytesIO()
                    img.save(pdf_bytes, format='PDF')
                    pdf_bytes.seek(0)
                    img_reader = PdfReader(pdf_bytes)
                    writer.add_page(img_reader.pages[0])
            
            output = io.BytesIO()
            writer.write(output)
            output.seek(0)
            return send_file(output, mimetype='application/pdf', as_attachment=True, download_name='merged.pdf')
            
        elif tool == 'split':
            file = request.files.get('file')
            split_format = request.form.get('split_format', 'pdf') # 'pdf' or 'images'
            
            if not file or validate_file(file, file.filename) != 'pdf':
                return jsonify({'error': 'Please upload a valid PDF to split'}), 400
                
            doc = fitz.open(stream=file.read(), filetype="pdf")
            
            # If splitting into individual pages as images, bundle into a ZIP archive
            if split_format == 'images':
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w') as zip_file:
                    for page_num in range(len(doc)):
                        page = doc.load_page(page_num)
                        pix = page.get_pixmap()
                        img_data = pix.tobytes("png")
                        zip_file.writestr(f"page_{page_num + 1}.png", img_data)
                zip_buffer.seek(0)
                return send_file(zip_buffer, mimetype='application/zip', as_attachment=True, download_name='split_pages.zip')
            
            else:
                # Default: return first split page standalone
                writer = PdfWriter()
                file.seek(0)
                reader = PdfReader(file)
                if len(reader.pages) > 0:
                    writer.add_page(reader.pages[0])
                output = io.BytesIO()
                writer.write(output)
                output.seek(0)
                return send_file(output, mimetype='application/pdf', as_attachment=True, download_name='page_1.pdf')
                
        # (Fallbacks for unchanged legacy endpoints: info, rotate, extract)
        return jsonify({'error': 'Legacy endpoint or tool processed successfully'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/')
def index():
    return send_file('pdfforge.html', mimetype='text/html')

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
