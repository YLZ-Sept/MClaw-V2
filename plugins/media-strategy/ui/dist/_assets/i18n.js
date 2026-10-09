window.MclawI18n = window.MclawI18n || {
  dict: {},
  register(dict){ this.dict = Object.assign(this.dict, dict || {}); },
  t(key){ return this.dict[key] || key; }
};
