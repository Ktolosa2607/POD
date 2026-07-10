import streamlit as st
import pymysql
import pandas as pd
from datetime import datetime
import base64
import qrcode
from io import BytesIO
from fpdf import FPDF
import os

# ==========================================
# CONFIGURACIÓN INICIAL
# ==========================================
st.set_page_config(page_title="Sistema de Recepción - POD", layout="wide")

# --- CONEXIÓN A TiDB ---
@st.cache_resource
def init_connection():
    return pymysql.connect(
        host=st.secrets["tidb"]["host"],
        port=st.secrets["tidb"]["port"],
        user=st.secrets["tidb"]["user"],
        password=st.secrets["tidb"]["password"],
        database=st.secrets["tidb"]["database"],
        autocommit=True
    )

conn = init_connection()

# --- INICIALIZACIÓN DE ESTADOS ---
if 'step' not in st.session_state:
    st.session_state.step = 1
if 'pod_data' not in st.session_state:
    st.session_state.pod_data = {'pallets': []}
if 'current_pallet' not in st.session_state:
    st.session_state.current_pallet = 1

# --- MENÚ LATERAL ---
menu = st.sidebar.radio("Navegación", ["Nueva Recepción (POD)", "Historial de PODs"])

# ==========================================
# PANTALLA 1: NUEVA RECEPCIÓN
# ==========================================
if menu == "Nueva Recepción (POD)":
    st.title("📦 Generación de POD de Recepción")

    # PASO 1: Datos Generales
    if st.session_state.step == 1:
        st.header("Paso 1: Información del Proveedor")
        provider_options = ["iMile", "J&T", "Forza", "Otro"]
        selected_provider = st.selectbox("Seleccione el proveedor", provider_options)
        
        if selected_provider == "Otro":
            provider = st.text_input("Escriba el nombre del proveedor")
        else:
            provider = selected_provider

        pallets_count = st.number_input("Cantidad total de pallets a recibir", min_value=1, step=1)

        if st.button("Siguiente ➡️"):
            if provider:
                st.session_state.pod_data['provider'] = provider
                st.session_state.pod_data['total_pallets'] = pallets_count
                st.session_state.step = 2
                st.rerun()
            else:
                st.warning("Por favor ingrese el nombre del proveedor.")

    # PASO 2: Revisión por Pallet
    elif st.session_state.step == 2:
        current = st.session_state.current_pallet
        total = st.session_state.pod_data['total_pallets']
        
        st.header(f"Paso 2: Revisión de Pallet {current} de {total}")
        
        box_count = st.number_input(f"Cantidad de cajas en el Pallet {current}", min_value=1, step=1)
        has_damage = st.radio("¿Hay cajas dañadas en este pallet?", ["No", "Sí"])
        
        photos = []
        if has_damage == "Sí":
            st.warning("📸 Por favor, tome fotos de las cajas dañadas antes de cerrar el pallet.")
            uploaded_files = st.file_uploader("Adjuntar fotos", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True)
            for file in uploaded_files:
                bytes_data = file.getvalue()
                base64_img = base64.b64encode(bytes_data).decode('utf-8')
                photos.append(base64_img)

        if st.button("Guardar Pallet y Continuar 📦"):
            st.session_state.pod_data['pallets'].append({
                'pallet_number': current,
                'box_count': box_count,
                'has_damage': True if has_damage == "Sí" else False,
                'photos': photos
            })
            
            if current < total:
                st.session_state.current_pallet += 1
            else:
                st.session_state.step = 3
            st.rerun()

    # PASO 3: Firmas y Generación
    elif st.session_state.step == 3:
        st.header("Paso 3: Confirmación y Firmas")
        
        st.write("### Resumen de Recepción")
        df_summary = pd.DataFrame(st.session_state.pod_data['pallets'])
        df_summary['Cantidad de Fotos'] = df_summary['photos'].apply(len)
        st.dataframe(df_summary[['pallet_number', 'box_count', 'has_damage', 'Cantidad de Fotos']])
        
        col1, col2 = st.columns(2)
        with col1:
            receiver_name = st.text_input("Nombre de quien recibe (Bodega)")
            receiver_sig = st.text_input("Firma de quien recibe (Iniciales)") 
        with col2:
            deliverer_name = st.text_input("Nombre de quien entrega (Proveedor)")
            deliverer_sig = st.text_input("Firma de quien entrega (Iniciales)")

        if st.button("✅ Generar y Guardar POD"):
            try:
                # Reconectar en caso de que la conexión a TiDB se haya puesto en reposo
                conn.ping(reconnect=True) 
                
                with conn.cursor() as cursor:
                    # 1. Insertar POD
                    sql_pod = "INSERT INTO pods (provider_name, total_pallets, receiver_name, deliverer_name, receiver_signature, deliverer_signature) VALUES (%s, %s, %s, %s, %s, %s)"
                    cursor.execute(sql_pod, (st.session_state.pod_data['provider'], st.session_state.pod_data['total_pallets'], receiver_name, deliverer_name, receiver_sig, deliverer_sig))
                    pod_id = cursor.lastrowid
                    
                    # 2. Insertar Pallets
                    for p in st.session_state.pod_data['pallets']:
                        sql_pallet = "INSERT INTO pallets (pod_id, pallet_number, box_count, has_damage) VALUES (%s, %s, %s, %s)"
                        cursor.execute(sql_pallet, (pod_id, p['pallet_number'], p['box_count'], p['has_damage']))
                        pallet_id = cursor.lastrowid
                        
                        # 3. Insertar Fotos
                        for photo_b64 in p['photos']:
                            sql_photo = "INSERT INTO damaged_photos (pallet_id, image_data) VALUES (%s, %s)"
                            cursor.execute(sql_photo, (pallet_id, photo_b64))
                
                st.success(f"¡POD #{pod_id} generada y guardada exitosamente en TiDB!")
                st.session_state.step = 1
                st.session_state.pod_data = {'pallets': []}
                st.session_state.current_pallet = 1
                
            except Exception as e:
                st.error(f"🚨 Error al guardar: {e}")

# ==========================================
# PANTALLA 2: HISTORIAL
# ==========================================
elif menu == "Historial de PODs":
    st.title("📜 Historial de Recepciones")
    
    query_params = st.query_params
    
    # 1. MODO VISOR DE FOTOS (Cuando se escanea el QR)
    if "pod_id" in query_params:
        target_pod = query_params["pod_id"]
        st.subheader(f"📸 Visor de Evidencias - POD #{target_pod}")
        
        try:
            conn.ping(reconnect=True)
            sql_fotos = """
                SELECT p.pallet_number, dp.image_data 
                FROM pallets p 
                JOIN damaged_photos dp ON p.id = dp.pallet_id 
                WHERE p.pod_id = %s
            """
            df_fotos = pd.read_sql(sql_fotos, conn, params=(target_pod,))
            
            if len(df_fotos) > 0:
                for index, row in df_fotos.iterrows():
                    st.write(f"**Pallet #{row['pallet_number']}**")
                    img_bytes = base64.b64decode(row['image_data'])
                    st.image(img_bytes, width=400)
            else:
                st.success("Este POD no tiene reportes de cajas dañadas.")
        except Exception as e:
            st.error(f"Error al cargar las fotos: {e}")
            
        if st.button("Volver al Historial General"):
            st.query_params.clear()
            st.rerun()
            
    # 2. MODO TABLA GENERAL Y DESCARGA DE PDF
    else:
        try:
            conn.ping(reconnect=True)
            df_pods = pd.read_sql("SELECT id as POD_ID, provider_name as Proveedor, total_pallets as Pallets, created_at as Fecha FROM pods ORDER BY id DESC", conn)
            st.dataframe(df_pods, use_container_width=True)
            
            selected_pod = st.selectbox("Seleccione un POD para ver detalles o imprimir", df_pods['POD_ID'])
            if selected_pod:
                
                # --- URL REAL DE LA APLICACIÓN ---
                MI_URL_STREAMLIT = "https://kphwfbxyb78gwczjjetjsf.streamlit.app"
                url_qr = f"{MI_URL_STREAMLIT}/?pod_id={selected_pod}"
                
                col_info, col_qr = st.columns([2, 1])
                with col_qr:
                    qr = qrcode.make(url_qr)
                    qr_img_path = f"qr_temp_{selected_pod}.png"
                    qr.save(qr_img_path)
                    st.image(qr.get_image(), caption=f"QR de Evidencias", width=150)
                
                with col_info:
                    st.markdown(f"**Leyenda Legal:** *Se reciben cajas cerradas no se han contado la cantidad de paquetes que se están recibiendo, por lo que luego de la revisión se contarán las piezas.*")
                    
                    # --- GENERAR PDF ---
                    pod_info = pd.read_sql("SELECT * FROM pods WHERE id = %s", conn, params=(selected_pod,)).iloc[0]
                    pallets_info = pd.read_sql("SELECT pallet_number, box_count, has_damage FROM pallets WHERE pod_id = %s", conn, params=(selected_pod,))
                    
                    pdf = FPDF()
                    pdf.add_page()
                    
                    # Titulo
                    pdf.set_font("Arial", 'B', 16)
                    pdf.cell(0, 10, f"PROOF OF DELIVERY (POD) #{selected_pod}", ln=True, align='C')
                    pdf.set_font("Arial", '', 11)
                    pdf.cell(0, 10, f"Fecha: {pod_info['created_at']}   |   Proveedor: {pod_info['provider_name']}   |   Pallets: {pod_info['total_pallets']}", ln=True, align='C')
                    pdf.ln(10)
                    
                    # Tabla
                    pdf.set_font("Arial", 'B', 10)
                    pdf.cell(40, 10, "Pallet #", border=1, align='C')
                    pdf.cell(40, 10, "Cajas", border=1, align='C')
                    pdf.cell(40, 10, "Danos?", border=1, align='C')
                    pdf.ln()
                    
                    pdf.set_font("Arial", '', 10)
                    for _, row in pallets_info.iterrows():
                        pdf.cell(40, 10, str(row['pallet_number']), border=1, align='C')
                        pdf.cell(40, 10, str(row['box_count']), border=1, align='C')
                        pdf.cell(40, 10, "Si" if row['has_damage'] else "No", border=1, align='C')
                        pdf.ln()
                    
                    pdf.ln(10)
                    
                    # Leyenda
                    pdf.set_font("Arial", 'I', 9)
                    leyenda = "Leyenda Legal: Se reciben cajas cerradas no se han contado la cantidad de paquetes que se estan recibiendo, por lo que luego de la revision se contaran las piezas correspondientes."
                    pdf.multi_cell(0, 5, leyenda)
                    pdf.ln(20)
                    
                    # Firmas
                    pdf.set_font("Arial", 'B', 10)
                    pdf.cell(90, 10, "_________________________", align='C')
                    pdf.cell(90, 10, "_________________________", align='C')
                    pdf.ln()
                    pdf.cell(90, 5, "Firma Quien Entrega", align='C')
                    pdf.cell(90, 5, "Firma Quien Recibe", align='C')
                    pdf.ln(20)
                    
                    # QR
                    pdf.image(qr_img_path, x=85, w=40)
                    pdf.cell(0, 10, "Escanee para ver fotos de evidencias", ln=True, align='C')
                    
                    # Descargar
                    pdf_bytes = pdf.output(dest='S').encode('latin1')
                    
                    st.download_button(
                        label="⬇️ Descargar POD en PDF",
                        data=pdf_bytes,
                        file_name=f"POD_Recepcion_{selected_pod}.pdf",
                        mime="application/pdf"
                    )
        except Exception as e:
            st.error(f"🚨 Error al cargar el historial: {e}")
