from langchain_community.document_loaders import CSVLoader, PyMuPDFLoader, PyPDFDirectoryLoader, PyPDFLoader, \
    TextLoader, \
    UnstructuredExcelLoader, UnstructuredHTMLLoader, \
    UnstructuredImageLoader, UnstructuredMarkdownLoader, UnstructuredPowerPointLoader, UnstructuredWordDocumentLoader
from langchain_text_splitters import CharacterTextSplitter, RecursiveCharacterTextSplitter


class DocLoader:
    """Load supported source files as LangChain documents."""

    def __init__(self):
        """Initialize a stateless document loader facade."""
        pass

    def txt_loader(self, filepath):
        """Load a UTF-8 text file as a list of documents."""
        loader = TextLoader(filepath, encoding='utf8')
        split_docs = loader.load()
        return split_docs

    def csv_loader(self, filepath):
        """Load a UTF-8 CSV file as a list of documents."""
        loader = CSVLoader(file_path=filepath, encoding='utf8')
        split_docs = loader.load()
        return split_docs

    def html_loader(self, filpath):
        """Load an HTML file as a list of documents."""
        loader = UnstructuredHTMLLoader(filpath)
        split_docs = loader.load()
        return split_docs

    def markdown_loader(self, filepath, mode='single'):
        """Load a Markdown file using the requested parsing mode."""
        loader = UnstructuredMarkdownLoader(filepath, mode=mode)
        split_docs = loader.load()
        return split_docs

    def pdf_loader(self, filepath, extract_images=True):
        """Load a PDF, optionally extracting text from embedded images."""
        if extract_images:
            loader = PyPDFLoader(filepath, extract_images=extract_images)
        else:
            loader = PyMuPDFLoader(filepath)  # 最快的 PDF 解析选项，但不能提取图片中的文字
        split_docs = loader.load_and_split()
        return split_docs


    def excel_loader(self, filepath, mode='single'):
        """Load an Excel workbook using the requested parsing mode."""
        loader = UnstructuredExcelLoader(filepath, mode=mode)
        split_docs = loader.load()
        return split_docs

    def ppt_loader(self, filepath, mode='single'):
        """Load a PowerPoint presentation using the requested parsing mode."""
        loader = UnstructuredPowerPointLoader(filepath, mode=mode)
        split_docs = loader.load()
        return split_docs


    def word_loader(cls, filepath, mode='single'):
        """Load a Word document using the requested parsing mode."""
        loader = UnstructuredWordDocumentLoader(filepath, mode=mode)
        split_docs = loader.load()
        return split_docs

    def img_loader(cls, filepath, mode='single'):
        """Load text extracted from an image using the requested mode."""
        loader = UnstructuredImageLoader(filepath, mode=mode)
        split_docs = loader.load()
        return split_docs


class TextSpliter():
    """Split LangChain documents into smaller text chunks."""

    @staticmethod
    def text_split_by_recursive_char(doc_list, separators=["\n\n", "\n", "。", "！", "？", " ", ""], chunk_size=500, chunk_overlap=20,
            length_function=len, keep_separator=True, is_separator_regex=False):
        """Recursively split documents while preserving their metadata."""
        text_splitter = RecursiveCharacterTextSplitter(
            separators=separators,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=length_function,
            keep_separator=keep_separator,
            is_separator_regex=is_separator_regex,
        )
        return text_splitter.split_documents(doc_list)

    @classmethod
    def text_split_by_char(cls, doc_list, separator='\n', chunk_size=500, chunk_overlap=20, length_function=len,
            is_separator_regex=False):
        """Split documents by one separator and preserve source metadata."""
        text_splitter = CharacterTextSplitter(
            separator=separator,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=length_function,
            is_separator_regex=is_separator_regex,
        )

        final_docs = []
        for doc in doc_list:
            docs = doc.page_content  # langchian 加载的 txt 转换为 str
            split_docs = text_splitter.create_documents([docs])
            for split_doc in split_docs:
                split_doc.metadata = doc.metadata
            final_docs.extend(split_docs)
        return final_docs
    
if __name__ == '__main__':
    loader = DocLoader()
    # split_docs = loader.word_loader(filepath=r"D:\MyProjects\Gitlab\ai_app_team\ai_custom_robot\doc_serve\docs\test.docx")
    split_docs = loader.ppt_loader(filepath=r"D:\Downloads\事例発表会_第二システム_張家旺_20220615.pptx")
    
    # print(split_docs)

    # split_docs_by_char = TextSpliter.text_split_by_char(doc_list=split_docs, chunk_size=200, chunk_overlap=20)
    split_docs_by_char = TextSpliter.text_split_by_recursive_char(doc_list=split_docs, chunk_size=200, chunk_overlap=20)
    print(split_docs_by_char)
