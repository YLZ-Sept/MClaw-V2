// ─── 供应商信息（产品简介 / 续费联系方式） ───
//
// 授权页展示用。写死在这里而非后端配置：只有一家供应商对外销售，
// 改动频率低于发版频率，多一层配置反而是负担。
//
// 若将来出现代理商、需要按客户显示不同售后联系人，再改为后端配置项
// 或写入授权码 payload——届时授权页只需换数据来源，展示逻辑不用动。
//
// 内容源：E:\桌面\产品信息.docx（2026-09-03）。
// 空字符串的字段在页面上自动隐藏，不会留下空行。

export interface VendorContact {
  company: string;
  phone?: string;
  address?: string;
  email?: string;
  website?: string;
  /** 微信客服会话链接（work.weixin.qq.com/kfid/...） */
  wechatService?: string;
  /** 微信服务号名称 */
  wechatAccount?: string;
  douyin?: string;
  xiaohongshu?: string;
  hours?: string;
}

export const VENDOR: VendorContact = {
  company: "云南米贝科技有限公司",
  phone: "0871-63820616",
  address: "云南省昆明市高新区昆百大国际派B座1703",
  email: "admin@ynmbkj.cn",
  website: "https://ynmbkj.cn",
  wechatService: "https://work.weixin.qq.com/kfid/kfc6ea1e452c2944b7f",
  wechatAccount: "云南米贝科技有限公司",
  douyin: "44264185979",
  xiaohongshu: "",
  hours: "",
};
