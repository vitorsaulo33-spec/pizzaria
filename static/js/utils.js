/**
 * utils.js - Utils Universal (Versão Final)
 */

let _managerData = [];
let _managerType = ''; 
let _managerEndpoint = ''; 

function openManager(dataFromHtml, type, title) {
    // BLINDAGEM DE DADOS
    if (!dataFromHtml) _managerData = [];
    else if (Array.isArray(dataFromHtml)) _managerData = dataFromHtml;
    else if (typeof dataFromHtml === 'string') {
        try { _managerData = JSON.parse(dataFromHtml); } 
        catch (e) { console.error("Erro parse:", e); _managerData = []; }
    } 
    else _managerData = [];

    _managerType = type;
    
    const isInventory = window.location.pathname.includes('inventory');
    if (type === 'category') {
        _managerEndpoint = isInventory ? '/admin/inventory/aux/save' : '/admin/menu/category/save';
    } else if (type === 'unit') {
        _managerEndpoint = '/admin/inventory/aux/save';
    }

    const titleEl = document.querySelector('#catManagerModal h3');
    if(titleEl) titleEl.innerText = title;
    
    document.getElementById('catManId').value = '';
    document.getElementById('catManName').value = '';
    document.getElementById('catManName').placeholder = `Novo item em ${title}...`;

    renderManagerList();

    const modal = document.getElementById('catManagerModal');
    const content = document.getElementById('catManagerContent');
    if(modal) {
        modal.classList.remove('hidden');
        setTimeout(() => { 
            if(content) { content.classList.remove('scale-95', 'opacity-0'); content.classList.add('scale-100', 'opacity-100'); }
        }, 10);
    }
}

function renderManagerList() {
    const list = document.getElementById('catManagerList');
    if (!list) return;
    
    list.innerHTML = '';

    // Blindagem
    if (!Array.isArray(_managerData)) {
        _managerData = []; 
    }

    if (_managerData.length === 0) {
        list.innerHTML = '<tr><td class="p-4 text-center text-slate-500 text-xs">Nenhum item cadastrado.</td></tr>';
    } else {
        _managerData.forEach(item => {
            // --- LÓGICA DO BADGE DE CONTAGEM ---
            let countBadge = '';
            if (item.item_count !== undefined) {
                const count = item.item_count;
                const colorClass = count > 0 ? 'text-green-400 bg-green-900/20 border-green-500/30' : 'text-slate-500 bg-slate-800 border-slate-700';
                countBadge = `<span class="ml-2 text-[10px] font-mono px-2 py-0.5 rounded border ${colorClass}">${count} itens</span>`;
            }
            // -----------------------------------

            const tr = document.createElement('tr');
            tr.className = 'hover:bg-slate-700/20 transition border-b border-slate-700/30 last:border-0';
            
            // Adicionamos o ${countBadge} ao lado do nome
            tr.innerHTML = `
                <td class="px-4 py-3 text-white font-medium text-sm flex items-center">
                    ${item.name}
                    ${countBadge}
                </td>
                <td class="px-4 py-3 text-right">
                    <button onclick="editManagerItem(${item.id}, '${item.name}')" class="text-blue-400 hover:text-blue-300 mr-2 p-2 transition"><i class="fas fa-edit"></i></button>
                    <button onclick="deleteManagerItem(${item.id})" class="text-red-400 hover:text-red-300 p-2 transition"><i class="fas fa-trash"></i></button>
                </td>
            `;
            list.appendChild(tr);
        });
    }
}

function editManagerItem(id, name) {
    document.getElementById('catManId').value = id;
    const el = document.getElementById('catManName');
    el.value = name;
    el.focus();
}

async function submitManagerItem(e) {
    e.preventDefault();
    const fd = new FormData();
    const id = document.getElementById('catManId').value;
    const name = document.getElementById('catManName').value;
    
    fd.append('name', name);
    
    const isInventory = window.location.pathname.includes('inventory');
    if(id) {
        const idField = (_managerType === 'category' && !isInventory) ? 'cat_id' : 'id';
        fd.append(idField, id);
    }
    if (isInventory) fd.append('type', _managerType);

    try {
        const res = await fetch(_managerEndpoint, {method:'POST', body:fd});
        if(res.ok) {
            // --- CORREÇÃO AJAX ---
            if (typeof window.onManagerUpdate === 'function') {
                await window.onManagerUpdate(_managerType); // Atualiza dados sem reload
                closeManagerModal();
                Swal.fire({toast: true, icon: 'success', title: 'Salvo!', position: 'top-end', timer: 1500, showConfirmButton: false});
            } else {
                location.reload(); // Fallback para páginas antigas (Estoque)
            }
            // ---------------------
        }
        else Swal.fire('Erro', 'Falha ao salvar', 'error');
    } catch(e) { Swal.fire('Erro', 'Conexão', 'error'); }
}

async function deleteManagerItem(id) {
    if(!confirm('Tem certeza?')) return;
    const isInventory = window.location.pathname.includes('inventory');
    let endpoint = '';

    if (_managerType === 'category') endpoint = isInventory ? `/admin/inventory/aux/category/${id}` : `/admin/menu/category/${id}`;
    else endpoint = `/admin/inventory/aux/unit/${id}`;

    try {
        const res = await fetch(endpoint, {method:'DELETE'});
        if(res.ok) {
            // --- CORREÇÃO AJAX ---
            if (typeof window.onManagerUpdate === 'function') {
                await window.onManagerUpdate(_managerType);
                closeManagerModal();
            } else {
                location.reload();
            }
            // ---------------------
        }
        else Swal.fire('Erro', 'Erro ao excluir.', 'error');
    } catch(e) { Swal.fire('Erro', 'Conexão', 'error'); }
}

function closeManagerModal() {
    const modal = document.getElementById('catManagerModal');
    if(modal) modal.classList.add('hidden');
}

// ==========================================
//        UTILITÁRIOS DE UI (A Função que Faltava!)
// ==========================================

function populateSelect(selectId, dataList) {
    const select = document.getElementById(selectId);
    if (!select) return;

    select.innerHTML = '<option value="">Selecione...</option>';
    
    if (Array.isArray(dataList)) {
        dataList.forEach(item => {
            const option = document.createElement('option');
            option.value = item.id;
            const unitLabel = item.unit ? ` (${item.unit})` : '';
            option.innerText = `${item.name}${unitLabel}`;
            select.appendChild(option);
        });
    }
}