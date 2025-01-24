from fastapi import FastAPI, File, UploadFile, HTTPException, Depends, Body  # Added Body
from fastapi.responses import JSONResponse
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate
from typing import List, Dict
import asyncio
from sqlalchemy.orm import Session
from fastapi.responses import StreamingResponse
import fitz  # PyMuPDF
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain.chains import RetrievalQA
from langchain.chains import ConversationalRetrievalChain
from langchain.memory import ConversationBufferMemory
import os
from dotenv import load_dotenv

from database import engine, SessionLocal, Document

load_dotenv()
app = FastAPI()

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.post("/upload/")
async def upload_pdf(file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        contents = await file.read()
        doc = fitz.open(stream=contents, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        
        db_document = Document(
            filename=file.filename,
            content=text
        )
        db.add(db_document)
        db.commit()
        db.refresh(db_document)
        
        return {"id": db_document.id, "filename": file.filename}
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing PDF: {str(e)}")

@app.post("/ask/{document_id}")
async def ask_question(
    document_id: int,
    data: Dict = Body(...),
    db: Session = Depends(get_db)
):
    try:
        question = data.get("question")
        history = data.get("history", [])
        
        google_api_key = os.getenv("GOOGLE_API_KEY")
        if not google_api_key:
            raise HTTPException(status_code=500, detail="Google API key not configured")

        document = db.query(Document).filter(Document.id == document_id).first()
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200
        )
        texts = text_splitter.split_text(document.content)
        
        embeddings = GoogleGenerativeAIEmbeddings(
            model="models/embedding-001",
            google_api_key=google_api_key
        )
        vector_store = FAISS.from_texts(texts, embeddings)
        
        llm = ChatGoogleGenerativeAI(
            model="gemini-1.5-pro",
            temperature=0.3,
            google_api_key=google_api_key,
            convert_system_message_to_human=True
        )

        # Create custom prompt with system message
        system_template = """You are an exceptionally polite and knowledgeable assistant designed to help users extract information from PDF documents. Always address the user respectfully and answer their questions as completely and accurately as possible based on the content of the document. If the answer requires clarification or additional details, politely ask for more information. If the user's query cannot be answered directly from the document, explain why and offer to assist further. Your tone should always be calm, courteous, and professional.you r developed by kartik
        
        Context: {context}
        History: {chat_history}"""
        
        messages = [
            SystemMessagePromptTemplate.from_template(system_template),
            HumanMessagePromptTemplate.from_template("{question}")
        ]
        
        qa_prompt = ChatPromptTemplate.from_messages(messages)

        memory = ConversationBufferMemory(
            memory_key="chat_history",
            return_messages=True
        )
        
        for msg in history:
            if msg["role"] == "user":
                memory.chat_memory.add_user_message(msg["content"])
            elif msg["role"] == "assistant":
                memory.chat_memory.add_ai_message(msg["content"])

        # Add custom prompt to the chain
        qa = ConversationalRetrievalChain.from_llm(
            llm=llm,
            retriever=vector_store.as_retriever(),
            memory=memory,
            chain_type="stuff",
            combine_docs_chain_kwargs={"prompt": qa_prompt}
        )

        result = qa({"question": question})
        
        return JSONResponse({
            "answer": result["answer"],
            "history": history + [
                {"role": "user", "content": question},
                {"role": "assistant", "content": result["answer"]}
            ]
        })
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing question: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)