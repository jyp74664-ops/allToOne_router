
    // 状态保存和读取函数
    function saveFilterState() {
      try {
        localStorage.setItem('hideZeroSuccess', document.getElementById('hideZeroSuccess').checked);
        const rhz = document.getElementById('routerHideZeroSuccess');
        if (rhz) localStorage.setItem('routerHideZeroSuccess', rhz.checked);
        localStorage.setItem('stabilityHours', document.getElementById('stabilityHours').value);
        const rsh = document.getElementById('routerStabilityHours');
        if (rsh) localStorage.setItem('routerStabilityHours', rsh.value);
        localStorage.setItem('stableFilter', document.getElementById('stableFilter').value);
        localStorage.setItem('providerFilter', document.getElementById('providerFilter').value);
        localStorage.setItem('stableSearch', document.getElementById('stableSearch').value);
        const rsf = document.getElementById('routerStableFilter');
        if (rsf) localStorage.setItem('routerStableFilter', rsf.value);
        const rsf2 = document.getElementById('routerSearchFilter');
        if (rsf2) localStorage.setItem('routerSearchFilter', rsf2.value);
      } catch (e) {
        // 忽略存储错误（例如，无痕模式下的限制）
        console.warn('Failed to save filter state:', e);
      }
    }

    function loadFilterState() {
      try {
        const hideZeroSuccess = localStorage.getItem('hideZeroSuccess');
        if (hideZeroSuccess !== null) {
          const el = document.getElementById('hideZeroSuccess');
          if (el) el.checked = (hideZeroSuccess === 'true');
        }
        // 最近多上时间内的稳定性过滤器
        const stabilityHours = localStorage.getItem('stabilityHours');
        if (stabilityHours !== null) {
          const el = document.getElementById('stabilityHours');
          if (el) el.value = stabilityHours;
        }
        const routerStabilityHours = localStorage.getItem('routerStabilityHours');
        if (routerStabilityHours !== null) {
          const el = document.getElementById('routerStabilityHours');
          if (el) el.value = routerStabilityHours;
        }

        const stableFilter = localStorage.getItem('stableFilter');
        if (stableFilter !== null) {
          const el = document.getElementById('stableFilter');
          if (el) el.value = stableFilter;
        }

        const providerFilter = localStorage.getItem('providerFilter');
        if (providerFilter !== null) {
          const el = document.getElementById('providerFilter');
          if (el) el.value = providerFilter;
        }

        const stableSearch = localStorage.getItem('stableSearch');
        if (stableSearch !== null) {
          const el = document.getElementById('stableSearch');
          if (el) el.value = stableSearch;
        }

        const routerStableFilter = localStorage.getItem('routerStableFilter');
        if (routerStableFilter !== null) {
          const el = document.getElementById('routerStableFilter');
          if (el) el.value = routerStableFilter;
        }

        const routerSearchFilter = localStorage.getItem('routerSearchFilter');
        if (routerSearchFilter !== null) {
          const el = document.getElementById('routerSearchFilter');
          if (el) el.value = routerSearchFilter;
        }
      } catch (e) {
        // 忽略读取错误
        console.warn('Failed to load filter state:', e);
      }
    }
  