"""Versioned image taxonomy shared by both classifiers and resource validation."""
from typing import Literal

ImageKind = Literal["visdoc", "structured_image", "info_rich_image", "visual_image"]
Workflow = Literal["visdoc", "structured_image", "info_rich_image", "visual_image", "text_image", "text"]
IMAGE_CATEGORIES = {
    "visdoc": "完整文档页，如 PDF、论文、报告页面截图；即使页面内含表格或图表仍属于文档页",
    "structured_image": "独立统计图、数值对比表、流程图或关系图；不包括完整文档页",
    "info_rich_image": "代码、日志、报错、评测结果或信息密集 UI 截图，关键可见文字承担主要信息；不包括完整文档页或独立图表",
    "visual_image": "风景、人物、插画、视觉设计或其他以视觉内容为主的图片；少量装饰文字不改变分类",
}
IMAGE_WORKFLOWS = frozenset(IMAGE_CATEGORIES)
ROUTING_POLICY_VERSION = "four-image-v1"
CLASSIFICATION_STATE = (
    "根据图片的主要信息和呈现形式选择唯一类别。依次判断：完整文档页、独立结构图表、"
    "关键文字为主的信息密集截图，最后是普通视觉图片。不要把所有含文字的图片当作文档。"
)
